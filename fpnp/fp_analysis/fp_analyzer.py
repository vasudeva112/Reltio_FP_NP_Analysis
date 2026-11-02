"""
Module A - Reltio False Positive analysis of the Potential Match Report.

For every potential-match pair (one report row = pair + rule):
  1. read both entity IDs
  2. identify the applicable match rule (report Rule column; rule name or URI; inferred when absent)
  3. take the attributes that rule uses (from the configuration - never hardcoded)
  4. take the values: entity JSON export when supplied (OV values, null states preserved),
     otherwise the report's src_/match_ columns
  5. compare every rule operand with the configured comparator (shared reltio_engine)
  6. attribute scores, 7. overall estimated similarity, 8. explanation + FP classification

Classification (analytical - hypotheses for review, never ground truth):
  Confirmed False Positive      steward NOT_A_MATCH
  Confirmed Genuine Duplicate   steward MATCH / MERGED
  Potential False Positive      a populated identifier / date / critical attribute conflicts, or the
                                estimated similarity is below fp.candidate_max_score, or the match rests
                                on null handling while compared values disagree
  Likely Genuine Duplicate      Python rule evaluation reproduces the match, similarity >= fp.genuine_min_score
                                and no conflict
  Requires Manual Review        everything in between (including pairs Python cannot reproduce)

Reltio's own relevance (ReltioRelevance column) is carried separately; the estimated similarity is
an analytical score, not Reltio's match score.
"""

import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from ..attribute_extractor import EntityRecord, record_from_report
from ..candidate_generation import EntityStore
from ..exceptions_log import LOG
from ..explain import risk_indicators
from ..false_positive_analyzer import _frequency
from ..report_reader import ReportRow
from ..rule_config_parser import MatchGroup, short_attr
from ..rule_evaluator import RuleEngine
from ..steward_decisions import MERGED, MATCH, NOT_A_MATCH, Decision
from ..reltio_engine.scoring_config import ScoringConfig
from ..reltio_engine.scoring_engine import MATCH as RULE_MATCH, PairScore, PairScorer
from .potential_match_parser import INFER_RULE

log = logging.getLogger("fpnp")

CONFIRMED_FP = "Confirmed False Positive"
CONFIRMED_DUP = "Confirmed Genuine Duplicate"
POTENTIAL_FP = "Potential False Positive"
REVIEW = "Requires Manual Review"
GENUINE = "Likely Genuine Duplicate"
NOT_EVALUATED = "Not Evaluated"
FP_CLASSES = [CONFIRMED_FP, POTENTIAL_FP, REVIEW, GENUINE, CONFIRMED_DUP, NOT_EVALUATED]


def _float(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass
class FPResult:
    pairs: List[dict] = field(default_factory=list)          # one per report row (pair + rule)
    attributes: List[dict] = field(default_factory=list)     # one per pair x rule operand
    stats: Counter = field(default_factory=Counter)
    rules: List[MatchGroup] = field(default_factory=list)
    config: Optional[ScoringConfig] = None
    seconds: float = 0.0


class ReltioFPAnalyzer:
    def __init__(self, groups: Sequence[MatchGroup], engine: RuleEngine, config: Optional[ScoringConfig] = None,
                 settings=None, steward: Optional[Dict[str, Decision]] = None,
                 store: Optional[EntityStore] = None):
        self.groups = list(groups)
        self.by_uri = {g.uri.lower(): g for g in self.groups}
        self.by_name = {g.name.lower(): g for g in self.groups}
        self.engine = engine
        self.cfg = config or ScoringConfig()
        self.s = settings
        self.steward = steward or {}
        self.store = store if store is not None and len(store) else None
        self.scorer = PairScorer(self.groups, engine, self.cfg)
        self.stats = Counter()

    # ------------------------------------------------------------------
    def _group(self, rule: str) -> Optional[MatchGroup]:
        r = (rule or "").strip().lower()
        return self.by_uri.get(r) or self.by_name.get(r.rsplit("/", 1)[-1])

    def _record(self, uri: str, etype: str, crosswalks: str, values: Dict[str, str]) -> EntityRecord:
        if self.store is not None:
            rec = self.store.get(uri)
            if rec is not None:
                return rec
            self.stats["entity_missing_from_export"] += 1
            LOG.add("ENTITY_NOT_IN_EXPORT", "Report entity not in the entity JSON - report values used", example=uri)
        return record_from_report(uri, etype, crosswalks, values)

    def _entity_type(self, r: ReportRow) -> str:
        if r.entity_type:
            return r.entity_type
        if self.store is not None:
            rec = self.store.get(r.src_uri)
            if rec is not None:
                return rec.entity_type
        types = {g.entity_type for g in self.groups}
        return types.pop() if len(types) == 1 else ""

    def analyze(self, rows: Sequence[ReportRow], progress: Optional[Callable[[int, int], None]] = None) -> FPResult:
        t0 = time.time()
        res = FPResult(rules=self.groups, config=self.cfg)
        freq, avg = _frequency(list(rows), self.store, self.groups, self.engine.ctx,
                               getattr(self.s, "high_frequency_min_distinct_ratio", 0.3))
        total = len(rows)
        for n, r in enumerate(rows, 1):
            self.stats["report_rows"] += 1
            if r.src_uri == r.match_uri:
                self.stats["same_entity_rows"] += 1
                continue
            etype = self._entity_type(r)
            g = None if r.rule_uri == INFER_RULE else self._group(r.rule_uri)
            inferred = False
            L = self._record(r.src_uri, etype, r.src_crosswalks, r.src_values)
            R = self._record(r.match_uri, etype, r.match_crosswalks, r.match_values)
            if g is None and r.rule_uri not in ("", INFER_RULE):
                LOG.add("RULE_NOT_IN_CONFIG", "Report rule not in the loaded match-rule configuration (or not "
                                              "selected) - rule inferred", group=r.rule_uri, severity="ERROR")
            if g is None:
                cands = [x for x in self.groups if not etype or x.entity_type == etype]
                if not cands:
                    res.pairs.append(self._unevaluated(r, "no configured rule for this entity type"))
                    continue
                ps = PairScorer.best([self.scorer.score(x, L, R, self.store is not None) for x in cands])
                g, inferred = ps.group, True
            else:
                ps = self.scorer.score(g, L, R, cache=self.store is not None)
            reltio_rel = _float(r.extra.get("ReltioRelevance"))
            ind = risk_indicators(ps.evaluation, g, freq, getattr(self.s, "high_frequency_entities", 5),
                                  getattr(self.s, "low_margin", 0.03), True, avg,
                                  getattr(self.s, "high_frequency_factor", 5.0), reltio_rel)
            row = self._pair_row(r, ps, L, reltio_rel, ind, inferred)
            res.pairs.append(row)
            for a in ps.attribute_rows():
                a["EntityA"], a["EntityB"] = r.src_uri, r.match_uri
                res.attributes.append(a)
            self.stats[row["FPClassification"]] += 1
            if progress and (n % 500 == 0 or n == total):
                progress(n, total)
        self.engine.clear_cache()
        res.stats = self.stats
        res.seconds = round(time.time() - t0, 1)
        return res

    # ------------------------------------------------------------------
    def _unevaluated(self, r: ReportRow, why: str) -> dict:
        self.stats[NOT_EVALUATED] += 1
        return {"PairID": r.key, "EntityA": r.src_uri, "EntityB": r.match_uri, "EntityType": r.entity_type,
                "MatchRule": r.rule_label or r.rule_uri, "MatchRuleURI": r.rule_uri,
                "ReltioMatchScore": r.extra.get("ReltioRelevance", ""), "FPClassification": NOT_EVALUATED,
                "Explanation": f"Not evaluated: {why}.", "SourceFile": r.source_file, "SourceRow": r.row_no}

    def classify(self, ps: PairScore, steward: Optional[Decision]) -> tuple:
        cfg = self.cfg.fp
        o = ps.outcome
        if steward is not None and steward.kind == NOT_A_MATCH:
            return CONFIRMED_FP, f"steward decision {steward.raw}"
        if steward is not None and steward.kind in (MATCH, MERGED):
            return CONFIRMED_DUP, f"steward decision {steward.raw}"
        reasons = []
        if ps.conflicts:
            reasons.append("conflicting populated values: " + ", ".join(ps.conflicts))
        if ps.cross_instance and o.matched:
            reasons.append("nested sub-attributes matched on different instances of "
                           + ", ".join(ps.cross_instance) + " (e.g. Type from one identifier, Value from another)")
        if ps.overall is not None and ps.overall < cfg.candidate_max_score:
            reasons.append(f"estimated similarity {ps.overall:.1f} < {cfg.candidate_max_score:g}")
        if ps.null_satisfied and ps.unmatched_attributes and len(ps.null_satisfied) >= len(ps.matched_attributes):
            reasons.append("match rests on null handling (" + ", ".join(ps.null_satisfied)
                           + ") while compared values disagree (" + ", ".join(ps.unmatched_attributes) + ")")
        if reasons:
            return POTENTIAL_FP, "; ".join(reasons)
        if ps.overall is None:
            return REVIEW, "no attribute with values on both entities - similarity cannot be estimated"
        if o.status == RULE_MATCH and ps.overall >= cfg.genuine_min_score:
            return GENUINE, (f"rule reproduced, estimated similarity {ps.overall:.1f} >= {cfg.genuine_min_score:g},"
                             " no conflicting values")
        why = []
        if o.status != RULE_MATCH:
            why.append(f"Python rule evaluation: {o.status}")
        if ps.overall < cfg.genuine_min_score:
            why.append(f"similarity {ps.overall:.1f} between {cfg.candidate_max_score:g} and {cfg.genuine_min_score:g}")
        return REVIEW, "; ".join(why) or "mixed evidence"

    def _pair_row(self, r: ReportRow, ps: PairScore, L: EntityRecord, reltio_rel: Optional[float], ind,
                  inferred: bool) -> dict:
        st = self.steward.get(r.key)
        st = st if st is not None and st.kind in (NOT_A_MATCH, MATCH, MERGED) else None
        cls, basis = self.classify(ps, st)
        g, o = ps.group, ps.outcome
        expl = self.explain(ps, r, cls, basis, inferred, L.origin)
        row = {
            "PairID": ps.pair_id, "EntityA": r.src_uri, "EntityB": r.match_uri, "EntityType": g.entity_type,
            "MatchRule": g.label, "MatchRuleName": g.name, "MatchRuleURI": g.uri, "RuleType": g.type,
            "RuleSource": "inferred (best explaining rule)" if inferred else "report",
            "ReltioMatchScore": "" if reltio_rel is None else reltio_rel,
            "ReltioMatchAction": r.extra.get("ReltioMatchAction", ""),
            "EstimatedSimilarityScore": None if ps.overall is None else round(ps.overall, 1),
            "SimilarityGroup": ps.similarity_group, "SimilarityBand": ps.band,
            "AnalyticalRuleEvaluation": o.status, "PythonRelevance": o.relevance, "PythonAction": o.action,
            "RuleFidelity": o.fidelity,
            "FPClassification": cls, "ClassificationBasis": basis,
            "MatchedAttributes": ", ".join(ps.matched_attributes),
            "UnmatchedAttributes": ", ".join(ps.unmatched_attributes),
            "VariantAttributes": ", ".join(ps.variant_attributes),
            "NullAttributes": ", ".join(ps.null_attributes),
            "NullSatisfiedConditions": ", ".join(ps.null_satisfied),
            "Conflicts": ", ".join(ps.conflicts),
            "TopContributors": ", ".join(ps.top_contributors),
            "RiskIndicators": ", ".join(k for k, _ in ind if k != "APPROXIMATION_DEPENDENT"),
            "StewardDecision": st.raw if st else "",
            "ValueSource": "entity JSON (OV)" if L.origin == "entity_export" else "report columns",
            "Explanation": expl,
            "RuleExplanation": o.explanation,
            "SourceCrosswalks": r.src_crosswalks, "MatchCrosswalks": r.match_crosswalks,
            "SourceFile": r.source_file, "SourceRow": r.row_no,
        }
        return row

    @staticmethod
    def explain(ps: PairScore, r: ReportRow, cls: str, basis: str, inferred: bool, origin: str) -> str:
        g = ps.group
        rows = [a for a in ps.attributes if a.role == "Match condition"]
        exact = [a.attribute for a in rows if a.result == "EXACT"]
        variant = [f"{a.attribute} ('{a.matched_a}' ~ '{a.matched_b}' via {a.comparator})"
                   for a in rows if a.result == "VARIANT_MATCH"]
        unmatched = [f"{a.attribute} ('{a.matched_a or a.value_a}' vs '{a.matched_b or a.value_b}')"
                     for a in rows if a.result in ("NO_MATCH", "PARTIAL")]
        nulls = [f"{a.attribute} ({a.operator}, {a.result.replace('_', ' ').lower()}, "
                 f"{'passes' if a.operator_passed else 'fails'})" for a in rows if a.result in ("BOTH_NULL", "ONE_NULL")]
        nots = [a for a in ps.attributes if a.role.startswith("NOT")]
        parts = [f"Reltio returned this pair under {g.label}" + (" (rule inferred: report has no rule)" if inferred
                                                                 else "") + "."]
        if exact:
            parts.append("Matched exactly: " + ", ".join(exact) + ".")
        if variant:
            parts.append("Matched as accepted variants: " + "; ".join(variant) + ".")
        if unmatched:
            parts.append("Did not match: " + "; ".join(unmatched) + ".")
        if nulls:
            parts.append("Null handling: " + "; ".join(nulls) + ".")
        if nots:
            parts.append("NOT conditions: " + "; ".join(
                f"{a.attribute} {'differs (required)' if a.result in ('NO_MATCH', 'PARTIAL') else 'is equal'}"
                for a in nots) + ".")
        if ps.top_contributors:
            parts.append("Similarity driven by: " + ", ".join(ps.top_contributors) + ".")
        parts.append(f"Estimated similarity {'n/a' if ps.overall is None else f'{ps.overall:.1f}/100'} "
                     f"({ps.similarity_group}); Python rule evaluation: {ps.outcome.status}.")
        if ps.cross_instance:
            parts.append("Nested sub-attributes matched on different instances of: "
                         + ", ".join(ps.cross_instance) + ".")
        if origin != "entity_export":
            parts.append("Values from report columns (missing/null/empty cannot be told apart).")
        parts.append(f"Verdict: {cls} - {basis}.")
        return " ".join(parts)


def probe_inputs(an: "ReltioFPAnalyzer", rows: Sequence[ReportRow]) -> list:
    """(group, left record, right record, Reltio relevance) for every row whose rule is configured -
    input for reltio_engine.null_semantics.probe."""
    out = []
    for r in rows:
        g = None if r.rule_uri == INFER_RULE else an._group(r.rule_uri)
        if g is None or r.src_uri == r.match_uri:
            continue
        et = g.entity_type
        out.append((g, an._record(r.src_uri, et, r.src_crosswalks, r.src_values),
                    an._record(r.match_uri, et, r.match_crosswalks, r.match_values),
                    _float(r.extra.get("ReltioRelevance"))))
    return out
