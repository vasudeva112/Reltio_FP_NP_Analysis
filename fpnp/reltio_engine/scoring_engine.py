"""
Pair scoring: attribute-wise scores, the overall estimated similarity and - separately -
the rule evaluation outcome.

  Overall similarity = sum(score_i * weight_i) / sum(weight_i)
      over the rule's positive match conditions only (operands under NOT, negativeRule operands
      and equals/in filters are reported but never scored; attributes with no value are excluded
      unless null_policy=score). Attributes the rule does not use are never read.

  Rule outcome = the Python model of the configured Reltio rule (existing RuleEngine):
      MATCH | NO MATCH (logic) | NO MATCH (below threshold) | NOT APPLICABLE (filter) |
      VETOED (negative rule) | UNSUPPORTED

A pair can score high and still fail the rule (e.g. one exact identifier differs), or pass the
rule with a modest score (e.g. exactOrNull satisfied by missing values). Both are reported.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..attribute_extractor import EntityRecord
from ..comparators.base import DOCUMENTED, UNSUPPORTED
from ..pair_deduplication import pair_key
from ..rule_config_parser import MatchGroup, short_attr
from ..rule_evaluator import GroupEvaluation, RuleEngine
from .comparator_engine import (BOTH_NULL_R, EXACT, MATCH_ROLE, NO_MATCH, ONE_NULL_R, PARTIAL, VARIANT,
                                AttributeComparison, ComparatorEngine)
from .scoring_config import ScoringConfig

MATCH = "MATCH"
NO_MATCH_LOGIC = "NO MATCH (rule logic not satisfied)"
NO_MATCH_THRESHOLD = "NO MATCH (relevance below every action threshold)"
NOT_APPLICABLE = "NOT APPLICABLE (filter not met)"
VETOED = "VETOED (negative rule)"
UNSUPPORTED_OUTCOME = "UNSUPPORTED (rule uses unimplemented classes)"


@dataclass
class RuleOutcome:
    status: str
    matched: bool
    logic_passed: bool
    applicable: bool
    relevance: Optional[float]
    action: str
    threshold: Optional[float]
    margin: Optional[float]
    negative_rule_hit: bool
    fidelity: str                    # Documented only / Approximation / Unsupported (decisive conditions)
    failed_conditions: List[str]
    null_driven: List[str]
    explanation: str


def rule_outcome(ev: GroupEvaluation, g: MatchGroup) -> RuleOutcome:
    lo = min((t.lo for t in g.thresholds), default=None) if g.is_relevance else None
    if ev.unsupported:
        status = UNSUPPORTED_OUTCOME
    elif ev.negative_rule_hit:
        status = VETOED
    elif not ev.applicable:
        status = NOT_APPLICABLE
    elif ev.matched:
        status = MATCH
    elif ev.relevance is not None:
        status = NO_MATCH_THRESHOLD
    else:
        status = NO_MATCH_LOGIC
    failed = []
    for t in ev.trace:
        lf = t.leaf
        if lf and not t.under_not and not lf.passed:
            failed.append(f"{lf.op}({short_attr(lf.attribute)})")
    nulls = ev.null_contributions()
    parts = [f"Rule {g.label} [{g.type}]: {status}."]
    if ev.relevance is not None:
        parts.append(f"Python relevance {ev.relevance:.4f}"
                     + (f" -> action '{ev.action_label or ev.action_type}'" if ev.action_type else "")
                     + (f"; lowest threshold {lo:g}" if lo is not None else "") + ".")
    if not ev.applicable:
        parts.append(ev.not_applicable_reason + ".")
    if ev.ignored_constraints:
        parts.append("Unmet filters not enforced for relevance scoring: " + "; ".join(ev.ignored_constraints) + ".")
    if failed and status != MATCH:
        parts.append("Failed conditions: " + ", ".join(failed) + ".")
    elif failed:
        parts.append("Conditions that failed without stopping the match: " + ", ".join(failed) + ".")
    if nulls:
        parts.append("Satisfied by null handling: " + ", ".join(nulls) + ".")
    fid = ev.decisive_status()
    if fid != DOCUMENTED:
        parts.append(f"Decisive conditions rely on: {fid}.")
    return RuleOutcome(status=status, matched=ev.matched, logic_passed=ev.logic_passed, applicable=ev.applicable,
                       relevance=ev.relevance,
                       action=f"{ev.action_type}:{ev.action_label}" if ev.action_type else "",
                       threshold=lo, margin=(round(ev.relevance - lo, 4) if ev.relevance is not None and lo is not None
                                             else None),
                       negative_rule_hit=ev.negative_rule_hit, fidelity=fid, failed_conditions=failed,
                       null_driven=nulls, explanation=" ".join(parts))


@dataclass
class PairScore:
    pair_id: str
    entity_a: str
    entity_b: str
    entity_type: str
    group: MatchGroup
    outcome: RuleOutcome
    overall: Optional[float]
    band: str
    similarity_group: str
    attributes: List[AttributeComparison]
    evaluation: GroupEvaluation
    cross_instance: List[str] = field(default_factory=list)

    # ---- derived views -------------------------------------------------
    def _match_rows(self) -> List[AttributeComparison]:
        return [a for a in self.attributes if a.role == MATCH_ROLE]

    @property
    def matched_attributes(self) -> List[str]:
        return [a.attribute for a in self._match_rows() if a.result in (EXACT, VARIANT)]

    @property
    def unmatched_attributes(self) -> List[str]:
        return [a.attribute for a in self._match_rows() if a.result in (NO_MATCH, PARTIAL)]

    @property
    def null_attributes(self) -> List[str]:
        return [f"{a.attribute} ({a.result})" for a in self._match_rows() if a.result in (BOTH_NULL_R, ONE_NULL_R)]

    @property
    def null_satisfied(self) -> List[str]:
        return [a.attribute for a in self._match_rows()
                if a.result in (BOTH_NULL_R, ONE_NULL_R) and a.operator_passed]

    @property
    def variant_attributes(self) -> List[str]:
        return [a.attribute for a in self._match_rows() if a.result == VARIANT]

    @property
    def conflicts(self) -> List[str]:
        return [f"{a.attribute} ({a.conflict})" for a in self.attributes if a.conflict]

    @property
    def top_contributors(self) -> List[str]:
        rows = sorted((a for a in self.attributes if a.in_score and a.contribution),
                      key=lambda a: -a.contribution)
        return [f"{a.attribute} +{a.contribution:.1f}" for a in rows[:5]]

    @property
    def compared_count(self) -> int:
        return sum(1 for a in self.attributes if a.in_score)

    def summary(self) -> dict:
        o = self.outcome
        return {
            "PairID": self.pair_id, "EntityA": self.entity_a, "EntityB": self.entity_b,
            "EntityType": self.entity_type, "MatchRule": self.group.label, "MatchRuleURI": self.group.uri,
            "RuleType": self.group.type,
            "EstimatedSimilarityScore": None if self.overall is None else round(self.overall, 1),
            "SimilarityBand": self.band, "SimilarityGroup": self.similarity_group,
            "RuleEvaluation": o.status, "RuleMatched": o.matched, "PythonRelevance": o.relevance,
            "PythonAction": o.action, "ThresholdLow": o.threshold, "MarginAboveThreshold": o.margin,
            "RuleFidelity": o.fidelity,
            "AttributesScored": self.compared_count,
            "MatchedAttributes": ", ".join(self.matched_attributes),
            "UnmatchedAttributes": ", ".join(self.unmatched_attributes),
            "NullAttributes": ", ".join(self.null_attributes),
            "VariantAttributes": ", ".join(self.variant_attributes),
            "Conflicts": ", ".join(self.conflicts),
            "CrossInstanceNested": ", ".join(self.cross_instance),
            "TopContributors": ", ".join(self.top_contributors),
            "FailedConditions": ", ".join(o.failed_conditions),
            "RuleExplanation": o.explanation,
        }

    def attribute_rows(self) -> List[dict]:
        return [a.as_row(self.pair_id) for a in self.attributes]


class PairScorer:
    """Scores entity pairs against match groups with the shared comparator engine."""

    def __init__(self, groups: Sequence[MatchGroup], engine: RuleEngine, config: Optional[ScoringConfig] = None):
        self.groups = list(groups)
        self.engine = engine
        self.cfg = config or ScoringConfig()
        self.cmp = ComparatorEngine(engine, self.cfg)

    def score(self, g: MatchGroup, left: EntityRecord, right: EntityRecord, cache: bool = False) -> PairScore:
        ev = self.engine.evaluate(g, left, right, cache=cache)
        pl, pr = self.engine.prepare(g, left, cache), self.engine.prepare(g, right, cache)
        attrs = [self.cmp.compare(g, t, pl, pr) for t in ev.trace if t.leaf is not None]
        num = den = 0.0
        for a in attrs:
            if a.in_score and a.score is not None:
                num += a.score * a.weight
                den += a.weight
        overall = round(num / den, 4) if den > 0 else None
        for a in attrs:
            if a.in_score and a.score is not None and den > 0:
                a.contribution = a.score * a.weight / den
        return PairScore(
            pair_id=pair_key(left.uri, right.uri), entity_a=left.uri, entity_b=right.uri,
            entity_type=g.entity_type, group=g, outcome=rule_outcome(ev, g), overall=overall,
            band=self.cfg.bands.label(overall) if overall is not None else "Not scored",
            similarity_group=self.cfg.similarity_group(overall), attributes=attrs, evaluation=ev,
            cross_instance=list(ev.cross_instance_attrs))

    def score_all(self, left: EntityRecord, right: EntityRecord, groups: Optional[Sequence[MatchGroup]] = None,
                  cache: bool = False) -> List[PairScore]:
        out = []
        for g in groups or self.groups:
            if left.entity_type and g.entity_type and left.entity_type != g.entity_type:
                continue
            out.append(self.score(g, left, right, cache))
        return out

    @staticmethod
    def best(scores: Sequence[PairScore]) -> Optional[PairScore]:
        """The most relevant rule for a pair: a matching rule first, then the highest similarity, then the rule
        whose result relies least on approximations and null handling."""
        if not scores:
            return None
        return max(scores, key=lambda s: (s.outcome.matched, s.overall if s.overall is not None else -1,
                                          s.outcome.fidelity == DOCUMENTED, -len(s.null_satisfied),
                                          s.outcome.relevance or 0))


def has_unsupported(ps: PairScore) -> bool:
    return ps.outcome.fidelity == UNSUPPORTED
