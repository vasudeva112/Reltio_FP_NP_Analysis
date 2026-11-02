"""
Per-attribute comparison with Reltio comparator semantics and a 0-100 attribute score.

The Reltio verdict for every operand comes from the existing operator/comparator layer
(operators.evaluate_* through RuleEngine.evaluate) - it is never recomputed here with a
generic string metric. This module adds, for each operand of the evaluated rule:

  * the comparator's normalised view of both values (normalization_engine.comparator_view)
  * a comparison result:  EXACT | VARIANT_MATCH | PARTIAL | NO_MATCH | BOTH_NULL | ONE_NULL |
                          CONSTRAINT_MET | CONSTRAINT_NOT_MET | UNSUPPORTED
  * an analytical similarity (0..1) of the closest value pair, typed (date / digits / text)
  * an attribute score 0-100 that depends on the comparator class:

      comparator accepted, identical after its normalisation -> bands.exact (100)
      comparator accepted different values (fuzzy / phonetic)  -> max(strong_min, similarity), max 99
      comparator rejected:
          exact-class comparator (BasicString, Phone, ...)     -> 0 ("zero", default) or graded
          fuzzy / phonetic comparator                           -> partial band if similarity >= floor,
                                                                   weak band if > 0, else 0
      no value on one / both sides                              -> excluded (null_policy=exclude) or
                                                                   one_null_score / both_null_score
      comparator class not implemented                          -> not scored, result UNSUPPORTED
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..attribute_extractor import Leaf
from ..cleansers import PreparedValues
from ..comparators.base import APPROXIMATION, UNSUPPORTED, EngineContext
from ..operators import BOTH_NULL, ONE_SIDED_NULL, OPERATOR_DOCS, LeafResult
from ..rule_config_parser import CONSTRAINT_OPS, MatchGroup, Node, attr_path_parts, short_attr
from ..rule_evaluator import RuleEngine, TraceRow
from .normalization_engine import best_similarity, view_list
from .scoring_config import ScoringConfig

EXACT, VARIANT, PARTIAL, NO_MATCH = "EXACT", "VARIANT_MATCH", "PARTIAL", "NO_MATCH"
BOTH_NULL_R, ONE_NULL_R = "BOTH_NULL", "ONE_NULL"
CONSTRAINT_MET, CONSTRAINT_NOT_MET, UNSUPPORTED_R = "CONSTRAINT_MET", "CONSTRAINT_NOT_MET", "UNSUPPORTED"
RESULT_MEANING = {
    EXACT: "Identical after the comparator's normalisation.",
    VARIANT: "Different values that the configured fuzzy / phonetic comparator accepted.",
    PARTIAL: "Comparator rejected the values, but they are textually close (near miss).",
    NO_MATCH: "Comparator rejected the values.",
    BOTH_NULL_R: "Neither entity has a value. Whether this passes depends on the operator (exactOrNull / "
                 "exactOrAllNull pass; exact / fuzzy fail unless BOTH_NULL_EXACT_MODE says otherwise).",
    ONE_NULL_R: "Only one entity has a value. exactOrNull passes; exactOrAllNull, exact and fuzzy fail.",
    CONSTRAINT_MET: "Filter (equals / in / notEquals) satisfied by both entities - not a similarity.",
    CONSTRAINT_NOT_MET: "Filter not satisfied by at least one entity - not a similarity.",
    UNSUPPORTED_R: "Comparator / operator class has no Python implementation; not scored, never claimed equal.",
}

MATCH_ROLE, NOT_ROLE, NEG_RULE_ROLE, FILTER_ROLE = ("Match condition", "NOT condition (must differ)",
                                                    "Negative rule", "Filter / constraint")

PHONETIC_CLASSES = {"DoubleMetaphoneComparator", "SoundexComparator", "MetaphoneComparator"}
FUZZY_CLASSES = {"JaroWinklerComparator", "DynamicDamerauLevenshteinDistance", "DamerauLevenshteinDistance",
                 "AddressLineComparator"}
# everything else (BasicString, PhoneNumber, RangeNumeric, Custom, CrossMulti, ...) is exact-class


def comparator_kind(name: str) -> str:
    if name in PHONETIC_CLASSES:
        return "phonetic"
    if name in FUZZY_CLASSES:
        return "fuzzy"
    return "exact"


@dataclass
class AttributeComparison:
    rule_uri: str
    rule_label: str
    attribute: str               # dotted path, e.g. Identifiers.Value
    attribute_uri: str
    operator: str
    role: str
    comparator: str
    comparator_kind: str
    comparator_status: str
    comparator_params: str
    value_a: str
    value_b: str
    norm_a: str
    norm_b: str
    matched_a: str
    matched_b: str
    operator_passed: bool
    result: str
    comparator_relevance: Optional[float]
    similarity: Optional[float]           # analytical 0..1
    value_kind: str
    score: Optional[float]                # 0-100, None = not scored
    band: str
    weight: float
    in_score: bool
    contribution: Optional[float] = None  # points of the overall score
    null_case: str = ""
    multi_value: str = ""
    nested: str = ""
    cleansing: str = ""
    detail: str = ""
    conflict: str = ""                    # e.g. "Identifier conflict"
    explanation: str = ""
    status: str = ""

    def as_row(self, pair_id: str = "") -> dict:
        return {
            "PairID": pair_id, "MatchRule": self.rule_label, "MatchRuleURI": self.rule_uri,
            "Attribute": self.attribute, "AttributeURI": self.attribute_uri, "Operator": self.operator,
            "Role": self.role, "Comparator": self.comparator, "ComparatorType": self.comparator_kind,
            "ComparatorParams": self.comparator_params, "EntityA_Value": self.value_a, "EntityB_Value": self.value_b,
            "EntityA_Normalized": self.norm_a, "EntityB_Normalized": self.norm_b,
            "ComparisonResult": self.result, "OperatorPassed": self.operator_passed,
            "ComparatorRelevance": None if self.comparator_relevance is None else round(self.comparator_relevance, 4),
            "AnalyticalSimilarity": None if self.similarity is None else round(self.similarity, 4),
            "AttributeScore": None if self.score is None else round(self.score, 1),
            "ScoreBand": self.band, "Weight": self.weight, "InOverallScore": self.in_score,
            "Contribution": None if self.contribution is None else round(self.contribution, 2),
            "NullCase": self.null_case, "MultiValue": self.multi_value, "Nested": self.nested,
            "Cleansing": self.cleansing, "Conflict": self.conflict, "ComparatorDetail": self.detail,
            "FidelityStatus": self.status, "Explanation": self.explanation,
        }


def _node_index(g: MatchGroup) -> Dict[str, Node]:
    idx: Dict[str, Node] = {}
    for root in (g.root, g.negative_root):
        if root is not None:
            for n in root.walk():
                idx[n.path] = n
    return idx


class ComparatorEngine:
    def __init__(self, engine: RuleEngine, config: Optional[ScoringConfig] = None):
        self.engine = engine
        self.ctx: EngineContext = engine.ctx
        self.cfg = config or ScoringConfig()
        self._nodes: Dict[str, Dict[str, Node]] = {}

    def nodes(self, g: MatchGroup) -> Dict[str, Node]:
        if g.uri not in self._nodes:
            self._nodes[g.uri] = _node_index(g)
        return self._nodes[g.uri]

    # ------------------------------------------------------------------
    def weight(self, g: MatchGroup, attr_uri: str) -> float:
        o = self.cfg.weight_override(short_attr(attr_uri), attr_uri)
        if o is not None:
            return o
        if self.cfg.weight_source == "rule" and g.is_relevance:
            return g.weight_for(attr_uri)
        return 1.0

    def _score(self, kind: str, passed: bool, identical: bool, sim: Optional[float],
               cmp_rel: Optional[float]) -> Optional[float]:
        b = self.cfg.bands
        s = max(x for x in (sim, cmp_rel if kind == "fuzzy" else None, 0.0) if x is not None)
        if passed:
            if identical:
                return b.exact
            return max(b.strong_min, min(b.exact - 1, round(100 * s, 1)))
        if kind == "exact" and self.cfg.exact_class_mismatch == "zero":
            return 0.0
        if s >= self.cfg.partial_similarity_floor:
            return max(b.partial_min, min(b.strong_min - 1, round(100 * s, 1)))
        if s > 0 and b.weak_min > 0:
            return max(b.weak_min, min(b.partial_min - 1, round(100 * s, 1)))
        return 0.0

    def compare(self, g: MatchGroup, trow: TraceRow, pl: Dict[str, PreparedValues],
                pr: Dict[str, PreparedValues]) -> AttributeComparison:
        lf: LeafResult = trow.leaf
        node = self.nodes(g).get(trow.path)
        attr_uri = lf.attribute
        if trow.path.startswith("negativeRule"):
            role = NEG_RULE_ROLE
        elif lf.is_constraint:
            role = FILTER_ROLE
        elif trow.under_not or lf.op in ("notExact", "notExactSame"):
            role = NOT_ROLE
        else:
            role = MATCH_ROLE

        # values (cleansed leaves as the comparator receives them)
        if lf.op == "multi" and node is not None:
            members = node.attributes
            lv = [v for a in members for v in pl[a].values]
            rv = [v for a in members for v in pr[a].values]
            cmp = self.engine.comparator(g, attr_uri, "CrossMultiComparator")
        else:
            lv = pl[attr_uri].values if attr_uri in pl else []
            rv = pr[attr_uri].values if attr_uri in pr else []
            cmp = self.engine.comparator(g, attr_uri) if not lf.is_constraint else None
        cmp_name = lf.comparator or (cmp.RELTIO_NAME if cmp is not None else "")
        kind = "filter" if lf.is_constraint else comparator_kind(cmp_name)
        norm_a = view_list(cmp, lv, self.ctx) if cmp is not None else " | ".join(self.ctx.norm(v) for v in lv)
        norm_b = view_list(cmp, rv, self.ctx) if cmp is not None else " | ".join(self.ctx.norm(v) for v in rv)

        sim, vkind, best_pair = (best_similarity(lv, rv, self.ctx) if lv and rv else (None, "", ("", "")))
        ma, mb = lf.matched_values if any(lf.matched_values) else best_pair
        weight = self.weight(g, attr_uri)
        rel_mode = g.is_relevance
        cmp_rel = lf.relevance if not lf.null_case else None
        null = lf.null_case

        # ---- result + score -----------------------------------------
        in_score = role == MATCH_ROLE
        if lf.is_constraint:
            result = CONSTRAINT_MET if lf.passed else CONSTRAINT_NOT_MET
            score, in_score = None, False
        elif lf.status == UNSUPPORTED or (cmp is not None and cmp.status == UNSUPPORTED):
            result, score, in_score = UNSUPPORTED_R, None, False
        elif null:
            result = BOTH_NULL_R if null == BOTH_NULL else ONE_NULL_R
            if self.cfg.null_policy == "score" and role == MATCH_ROLE:
                score = self.cfg.both_null_score if null == BOTH_NULL else self.cfg.one_null_score
            else:
                score, in_score = None, False
        else:
            equal = lf.passed if role == MATCH_ROLE else (not lf.passed if lf.op in ("notExact", "notExactSame")
                                                          else lf.passed)
            # equality of the underlying comparison (for NOT / negative-rule rows lf.passed is the operand's
            # own result before negation by the parent)
            identical = bool(ma or mb) and self.ctx.norm(ma) == self.ctx.norm(mb)
            if equal:
                result = EXACT if identical else VARIANT
            elif kind == "exact" and self.cfg.exact_class_mismatch == "zero":
                result = NO_MATCH                         # exact-class verdict; near miss shown in the explanation
            else:
                result = PARTIAL if (sim or 0) >= self.cfg.partial_similarity_floor else NO_MATCH
            score = self._score(kind, equal, identical, sim, cmp_rel)
        band = self.cfg.bands.label(score) if score is not None else (
            "Filter" if lf.is_constraint else "Not scored")

        # ---- context notes ------------------------------------------
        multi = ""
        if len(lv) > 1 or len(rv) > 1:
            multi = f"{len(lv)} x {len(rv)} values; best pair used ({ma} ~ {mb})"
        parts = attr_path_parts(attr_uri)
        nested = f"sub-attribute of {parts[0]}" if len(parts) > 1 else ""
        cleansing = ", ".join(lf.cleanse_notes)
        conflict = ""
        if role == MATCH_ROLE and not null and result in (NO_MATCH, PARTIAL) and lv and rv:
            dotted = short_attr(attr_uri)
            if self.cfg.is_critical(dotted, attr_uri):
                conflict = "Critical attribute conflict"
            elif self.cfg.auto_detect_identifiers and kind == "exact" and vkind == "digits":
                conflict = "Identifier/code conflict"
            elif vkind == "date" and (sim or 0) < 0.85:
                conflict = "Date conflict"

        ac = AttributeComparison(
            rule_uri=g.uri, rule_label=g.label, attribute=short_attr(attr_uri), attribute_uri=attr_uri,
            operator=lf.op, role=role, comparator=cmp_name, comparator_kind=kind,
            comparator_status=lf.comparator_status or (cmp.status if cmp is not None else ""),
            comparator_params=lf.comparator_params or (str(lf.values) if hasattr(lf, "values") else ""),
            value_a=lf.left, value_b=lf.right, norm_a=norm_a, norm_b=norm_b, matched_a=ma, matched_b=mb,
            operator_passed=lf.passed, result=result,
            comparator_relevance=cmp_rel if (rel_mode or kind == "fuzzy") else None,
            similarity=sim, value_kind=vkind, score=score, band=band, weight=weight, in_score=in_score,
            null_case=null, multi_value=multi, nested=nested, cleansing=cleansing, detail=lf.detail,
            conflict=conflict, status=lf.status)
        if lf.is_constraint and node is not None:
            ac.comparator_params = f"{lf.op} {node.values}"
        ac.explanation = self.explain(ac)
        return ac

    # ------------------------------------------------------------------
    @staticmethod
    def explain(a: AttributeComparison) -> str:
        va, vb = a.value_a, a.value_b
        if a.result in (CONSTRAINT_MET, CONSTRAINT_NOT_MET):
            return (f"Filter {a.comparator_params} on {a.attribute}: "
                    f"{'met by both entities' if a.result == CONSTRAINT_MET else 'NOT met'} ({a.detail}). "
                    "Filters restrict which entities the rule applies to; they are not part of the similarity.")
        if a.result == UNSUPPORTED_R:
            return f"{a.comparator or a.operator} is not implemented in Python - not scored and never treated as equal."
        op_doc = OPERATOR_DOCS.get(a.operator, {}).get("nulls", "")
        if a.result in (BOTH_NULL_R, ONE_NULL_R):
            who = "Neither entity has" if a.result == BOTH_NULL_R else "Only one entity has"
            s = (f"{who} a value for {a.attribute} ({va} vs {vb}). Operator {a.operator}: "
                 f"{'PASSES' if a.operator_passed else 'FAILS'} - {op_doc}")
            if a.role == MATCH_ROLE and a.operator_passed:
                s += " The rule is satisfied on this attribute by null handling, not by matching data."
            if not a.in_score:
                s += " Excluded from the similarity score (null_policy=exclude)."
            return s
        how = {
            EXACT: "identical after normalisation",
            VARIANT: f"different values accepted by {a.comparator}",
            PARTIAL: f"rejected by {a.comparator}, but close (similarity {a.similarity:.2f})" if a.similarity else "",
            NO_MATCH: f"rejected by {a.comparator}",
        }.get(a.result, a.result)
        s = f"{a.attribute}: '{a.matched_a or va}' vs '{a.matched_b or vb}' - {how}"
        if a.detail and a.result != EXACT:
            s += f" ({a.detail})"
        s += "."
        if a.role == NOT_ROLE:
            s += (" This is a NOT condition: the rule requires the values to DIFFER, so it "
                  f"{'passes' if a.result in (NO_MATCH, PARTIAL) else 'fails'} here; not part of the similarity.")
        elif a.role == NEG_RULE_ROLE:
            s += " Part of the negativeRule: when the negative rule is satisfied it blocks the match."
        elif a.score is not None:
            s += f" Attribute score {a.score:g} ({a.band.lower()})."
            if a.result == NO_MATCH and a.comparator_kind == "exact" and a.similarity and a.similarity >= 0.7:
                s += f" Near miss: values are {a.similarity:.0%} similar but {a.comparator} needs equality."
        if a.multi_value:
            s += f" Multi-value: {a.multi_value}."
        if a.conflict:
            s += f" {a.conflict}: both entities hold different populated values."
        if a.status == APPROXIMATION:
            s += " (Python behaviour for this comparator/operator is an approximation of Reltio.)"
        return s
