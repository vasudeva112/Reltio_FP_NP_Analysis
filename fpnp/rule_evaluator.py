"""
Evaluates match groups for an entity pair.

  * suspect / automatic / automatic_grouping -> boolean logic
  * relevance_based -> relevance score (weighted AND, max OR, 1-r NOT) + actionThresholds
  * negativeRule (if configured) vetoes the match
  * strict constraints (equals/in/notEquals/notIn) not met -> group not applicable

Comparator evaluation lives in comparators/, operator semantics in operators.py.
"""

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import comparator_registry as registry
from .attribute_extractor import EntityRecord
from .cleansers import GroupCleansers, PreparedValues
from .comparators.base import APPROXIMATION, DOCUMENTED, UNSUPPORTED, Comparator, EngineContext
from .exceptions_log import LOG
from .operators import (OPERATOR_DOCS, RELEVANCE_NULL_STATUS, LeafResult, evaluate_attribute_op,
                        evaluate_constraint, evaluate_multi)
from .rule_config_parser import (ATTRIBUTE_OPS, CONSTRAINT_OPS, MatchGroup, Node, Threshold,
                                 attr_path_parts, short_attr)

_EPS = 1e-9
_ACTION_PRECEDENCE = {"queue_for_review": 0, "potential_match": 0, "suspect": 0, "auto_merge": 1}


@dataclass
class TraceRow:
    path: str
    depth: int
    kind: str
    label: str
    passed: bool
    relevance: Optional[float]
    weight: Optional[float]
    leaf: Optional[LeafResult] = None
    under_not: bool = False

    def text(self) -> str:
        pad = "  " * self.depth
        res = "PASS" if self.passed else "FAIL"
        rel = f" r={self.relevance:.3f}" if self.relevance is not None else ""
        w = f" w={self.weight:g}" if self.weight is not None else ""
        if not self.leaf:
            return f"{pad}{self.label}: {res}{rel}{w}"
        lf = self.leaf
        extra = []
        if lf.comparator:
            extra.append(lf.comparator)
        if lf.null_case:
            extra.append(lf.null_case)
        if lf.detail and lf.detail != lf.null_case:
            extra.append(lf.detail)
        if lf.cleanse_notes:
            extra.append(",".join(lf.cleanse_notes))
        return (f"{pad}{self.label}: {res}{rel}{w} | L={lf.left} | R={lf.right}"
                + (f" | {'; '.join(extra)}" if extra else ""))

    def as_dict(self) -> dict:
        d = {"path": self.path, "condition": self.label, "passed": self.passed,
             "relevance": self.relevance, "weight": self.weight, "under_not": self.under_not}
        if self.leaf:
            lf = self.leaf
            d.update({"operator": lf.op, "attribute": short_attr(lf.attribute), "left": lf.left,
                      "right": lf.right, "left_state": lf.left_state, "right_state": lf.right_state,
                      "comparator": lf.comparator, "comparator_params": lf.comparator_params,
                      "comparator_status": lf.comparator_status, "null_case": lf.null_case,
                      "matched_values": list(lf.matched_values), "detail": lf.detail,
                      "cleansers": list(lf.cleanse_notes), "status": lf.status})
        return d


@dataclass
class GroupEvaluation:
    group_uri: str
    group_label: str
    group_type: str
    matched: bool = False
    logic_passed: bool = False
    applicable: bool = True
    not_applicable_reason: str = ""
    relevance: Optional[float] = None
    action_type: str = ""
    action_label: str = ""
    threshold_lo: Optional[float] = None
    negative_rule_hit: bool = False
    unsupported: bool = False
    trace: List[TraceRow] = field(default_factory=list)
    cross_instance_attrs: List[str] = field(default_factory=list)
    ignored_constraints: List[str] = field(default_factory=list)   # unmet but not vetoed (ignore mode)

    # ---- derived views -------------------------------------------------
    def leaves(self, positive_only: bool = True) -> List[LeafResult]:
        return [t.leaf for t in self.trace if t.leaf and not (positive_only and t.under_not)]

    def attribute_leaves(self) -> List[LeafResult]:
        return [l for l in self.leaves() if not l.is_constraint]

    def contributing(self) -> List[str]:
        return [short_attr(l.attribute) for l in self.attribute_leaves() if l.passed]

    def failed(self) -> List[str]:
        return [short_attr(l.attribute) for l in self.attribute_leaves() if not l.passed]

    def differing(self) -> List[str]:
        return [short_attr(l.attribute) for l in self.attribute_leaves() if l.values_differ]

    def null_contributions(self) -> List[str]:
        return [f"{short_attr(l.attribute)}:{l.op}:{l.null_case}" for l in self.attribute_leaves()
                if l.null_case and l.passed]

    def decisive_status(self) -> str:
        """Approximation if any condition that made the group pass relies on an approximation."""
        statuses: Set[str] = set()
        for l in self.leaves():
            if l.passed:
                statuses.add(l.status)
                if l.null_case and self.relevance is not None:
                    statuses.add(RELEVANCE_NULL_STATUS)
        if self.unsupported:
            return UNSUPPORTED
        if self.relevance is not None and self.ignored_constraints:
            statuses.add(APPROXIMATION)
        return APPROXIMATION if APPROXIMATION in statuses else DOCUMENTED

    def trace_text(self) -> str:
        head = (f"{self.group_label} [{self.group_type}] matched={self.matched}"
                + (f" relevance={self.relevance:.4f}" if self.relevance is not None else "")
                + (f" action={self.action_type}:{self.action_label}" if self.action_type else "")
                + ("" if self.applicable else f" NOT_APPLICABLE({self.not_applicable_reason})")
                + (f" UNMET_CONSTRAINTS_IGNORED({'; '.join(self.ignored_constraints)})"
                   if self.ignored_constraints else "")
                + (" NEGATIVE_RULE_HIT" if self.negative_rule_hit else ""))
        return "\n".join([head] + [t.text() for t in self.trace])

    def trace_json(self) -> str:
        return json.dumps([t.as_dict() for t in self.trace], default=str)


# ----------------------------------------------------------------------
def _as_bool(v, default=True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1")


class RuleEngine:
    def __init__(self, groups: List[MatchGroup], ctx: Optional[EngineContext] = None):
        self.groups = groups
        self.ctx = ctx or EngineContext()
        self._cleansers: Dict[str, GroupCleansers] = {}
        self._cmp: Dict[Tuple[str, str], Comparator] = {}
        self._prep_cache: Dict[Tuple[str, str], Dict[str, PreparedValues]] = {}
        for g in groups:
            self._cleansers[g.uri] = GroupCleansers(g, self.ctx)

    # ---- comparators / values ------------------------------------------
    def comparator(self, g: MatchGroup, attr: str, default_class: str = "") -> Comparator:
        key = (g.uri, attr)
        c = self._cmp.get(key)
        if c is None:
            m = g.comparators.get(attr)
            if m is None and default_class:
                c = registry.create(default_class, {}, self.ctx, g.uri, attr)
            else:
                c = registry.for_mapping(m, self.ctx, g.uri, attr)
            self._cmp[key] = c
        return c

    def prepare(self, g: MatchGroup, rec: EntityRecord, cache: bool = False) -> Dict[str, PreparedValues]:
        key = (rec.uri, g.uri)
        if cache and key in self._prep_cache:
            return self._prep_cache[key]
        cl = self._cleansers[g.uri]
        out = {a: cl.prepare(a, rec.get(a, g.use_ov_only)) for a in g.attributes()}
        if cache:
            if len(self._prep_cache) > 200_000:
                self._prep_cache.clear()
            self._prep_cache[key] = out
        return out

    def clear_cache(self) -> None:
        self._prep_cache.clear()

    # ---- evaluation -----------------------------------------------------
    def evaluate(self, g: MatchGroup, left: EntityRecord, right: EntityRecord,
                 cache: bool = False) -> GroupEvaluation:
        pl, pr = self.prepare(g, left, cache), self.prepare(g, right, cache)
        ev = GroupEvaluation(g.uri, g.label, g.type)
        rel_mode = g.is_relevance
        na: List[str] = []
        passed, rel = self._eval(g, g.root, pl, pr, rel_mode, 0, False, ev.trace, na, ev, top=True)
        ev.logic_passed = passed
        if na:
            ev.applicable = False
            ev.not_applicable_reason = "; ".join(na)

        if rel_mode:
            ev.relevance = round(rel, 6)
            t = self._threshold(g.thresholds, ev.relevance)
            if t is not None:
                ev.action_type, ev.action_label, ev.threshold_lo = t.type, t.label, t.lo
            ev.matched = ev.applicable and t is not None
        else:
            ev.matched = passed

        if g.negative_root is not None:
            neg_trace: List[TraceRow] = []
            npassed, _ = self._eval(g, g.negative_root, pl, pr, False, 1, False, neg_trace, [], ev, top=False)
            ev.trace.append(TraceRow("negativeRule", 0, "negativeRule", "NEGATIVE RULE", npassed, None, None))
            for t in neg_trace:
                t.under_not = True
            ev.trace.extend(neg_trace)
            if npassed and ev.matched:
                ev.negative_rule_hit = True
                ev.matched = False
        if ev.unsupported:
            LOG.add("UNSUPPORTED_EVALUATION", "Group contains unsupported operators/classes; "
                                              "results are not reliable", group=g.uri)
        self._cross_instance(ev)
        return ev

    def evaluate_all(self, left: EntityRecord, right: EntityRecord, groups: Optional[List[MatchGroup]] = None,
                     cache: bool = False) -> List[GroupEvaluation]:
        out = []
        for g in groups or self.groups:
            if left.entity_type and g.entity_type and left.entity_type != g.entity_type:
                continue
            out.append(self.evaluate(g, left, right, cache))
        return out

    @staticmethod
    def _threshold(thresholds: List[Threshold], r: float) -> Optional[Threshold]:
        cands = [t for t in thresholds if t.lo - _EPS <= r <= t.hi + _EPS]
        if not cands:
            return None
        return min(cands, key=lambda t: (_ACTION_PRECEDENCE.get(t.type.lower(), 0), -t.lo))

    def _leaf(self, g, node: Node, pl, pr, ev: GroupEvaluation) -> LeafResult:
        if not node.supported:
            ev.unsupported = True
            return LeafResult(op=node.op, attribute=node.attribute, passed=False, relevance=0.0,
                              detail="unsupported operator", status=UNSUPPORTED)
        if node.op in CONSTRAINT_OPS:
            return evaluate_constraint(node.op, node.attribute, node.values, self.ctx,
                                       pl[node.attribute], pr[node.attribute])
        if node.op == "multi":
            gcmp = self.comparator(g, node.attribute, "CrossMultiComparator")
            members = [self.comparator(g, a) for a in node.attributes]
            return evaluate_multi(node.attribute, node.attributes, gcmp, members, self.ctx,
                                  [pl[a] for a in node.attributes], [pr[a] for a in node.attributes])
        if node.op in ATTRIBUTE_OPS:
            cmp = self.comparator(g, node.attribute)
            if cmp.status == UNSUPPORTED:
                ev.unsupported = True
            return evaluate_attribute_op(node.op, node.attribute, cmp, self.ctx,
                                         pl[node.attribute], pr[node.attribute])
        ev.unsupported = True
        return LeafResult(op=node.op, attribute=node.attribute, passed=False, relevance=0.0,
                          detail="unknown operator", status=UNSUPPORTED)

    def _eval(self, g: MatchGroup, node: Node, pl, pr, rel_mode: bool, depth: int, under_not: bool,
              trace: List[TraceRow], na: List[str], ev: GroupEvaluation, top: bool,
              weight: Optional[float] = None, relaxed: bool = False) -> Tuple[bool, float]:
        """under_not: inside a NOT (trace flag). relaxed: inside OR/NOT, where constraint
        strictness is not applied group-wide (Approximation)."""
        if node.kind == "op":
            lf = self._leaf(g, node, pl, pr, ev)
            trace.append(TraceRow(node.path, depth, "op", node.describe(), lf.passed,
                                  lf.relevance if rel_mode else None, weight, lf, under_not))
            return lf.passed, lf.relevance

        row = TraceRow(node.path, depth, node.kind, node.kind.upper(), False, None, weight, None, under_not)
        trace.append(row)

        if node.kind == "not":
            p, r = self._eval(g, node.children[0], pl, pr, rel_mode, depth + 1, True, trace, na, ev,
                              False, 1.0 if rel_mode else None, True)
            row.passed, rel = (not p), 1.0 - r
            row.relevance = rel if rel_mode else None
            return row.passed, rel

        kids_relaxed = relaxed or node.kind == "or"
        results = []
        for c in node.children:
            w = self._weight(g, c, rel_mode)
            p, r = self._eval(g, c, pl, pr, rel_mode, depth + 1, under_not, trace, na, ev, False,
                              w if rel_mode else None, kids_relaxed)
            results.append((c, p, r, w))

        if node.kind == "and":
            row.passed = all(p for _, p, _, _ in results)
            num = den = 0.0
            nested: Dict[str, List[Tuple[float, float]]] = {}
            for c, p, r, w in results:
                if c.kind == "op" and c.op in CONSTRAINT_OPS:
                    strict = _as_bool(c.params.get("strict"), True)
                    if not p and strict and not relaxed:
                        if rel_mode and self.ctx.relevance_constraint_mode == "ignore":
                            ev.ignored_constraints.append(c.describe())
                        else:
                            na.append(f"{c.describe()} not satisfied")
                    if not rel_mode:
                        continue
                elif (rel_mode and c.kind == "op" and self.ctx.relevance_nested_aggregation != "flat"
                      and len(attr_path_parts(c.attribute)) > 1):
                    nested.setdefault(attr_path_parts(c.attribute)[0], []).append((w, r))
                    continue
                num += w * r
                den += w
            for parts in nested.values():            # nested attribute scored as one operand
                w = max(x for x, _ in parts)
                rs = [y for _, y in parts]
                r = max(rs) if self.ctx.relevance_nested_aggregation == "max" else sum(rs) / len(rs)
                num += w * r
                den += w
            rel = (num / den) if den > 0 else (1.0 if row.passed else 0.0)
        else:  # or
            row.passed = any(p for _, p, _, _ in results)
            rel = max((w * r for _, _, r, w in results), default=0.0)
        row.relevance = rel if rel_mode else None
        return row.passed, rel

    def what_if(self, g: MatchGroup, rel: Optional[Dict[str, float]] = None,
                failing: Sequence[str] = ()) -> Tuple[bool, float]:
        """(logic passed, relevance) of g's rule when each attribute operand has the relevance given in
        `rel` (default 1.0) and the attributes in `failing` have relevance 0. Constraints pass and the
        negativeRule is not applied. Same AND / OR / NOT, weight and nested aggregation as _eval, so
        what_if(g, actual leaf relevances) reproduces evaluate() for pairs whose constraints pass."""
        vals = dict(rel or {})
        vals.update({a: 0.0 for a in failing})
        rel_mode = g.is_relevance

        def walk(node: Node) -> Tuple[bool, float]:
            if node.kind == "op":
                if node.op in CONSTRAINT_OPS:
                    return True, 1.0
                r = vals.get(node.attribute, 1.0)
                return r > 0, r
            if node.kind == "not":
                p, r = walk(node.children[0])
                return not p, 1.0 - r
            results = [(c, *walk(c), self._weight(g, c, rel_mode)) for c in node.children]
            if node.kind == "or":
                return any(p for _, p, _, _ in results), max((w * r for _, _, r, w in results), default=0.0)
            num = den = 0.0
            nested: Dict[str, List[Tuple[float, float]]] = {}
            for c, p, r, w in results:
                if c.kind == "op" and c.op in CONSTRAINT_OPS:
                    if not rel_mode:
                        continue
                elif (rel_mode and c.kind == "op" and self.ctx.relevance_nested_aggregation != "flat"
                      and len(attr_path_parts(c.attribute)) > 1):
                    nested.setdefault(attr_path_parts(c.attribute)[0], []).append((w, r))
                    continue
                num += w * r
                den += w
            for parts in nested.values():
                w = max(x for x, _ in parts)
                rs = [y for _, y in parts]
                r = max(rs) if self.ctx.relevance_nested_aggregation == "max" else sum(rs) / len(rs)
                num += w * r
                den += w
            passed = all(p for _, p, _, _ in results)
            return passed, (num / den) if den > 0 else (1.0 if passed else 0.0)

        passed, r = walk(g.root)
        if rel_mode:
            return self._threshold(g.thresholds, round(r, 6)) is not None, r
        return passed, r

    @staticmethod
    def _weight(g: MatchGroup, node: Node, rel_mode: bool) -> float:
        if not rel_mode:
            return 1.0
        if node.kind != "op":
            return 1.0                                   # logical sub-operand (documented)
        if node.op in CONSTRAINT_OPS:
            try:
                return float(node.params.get("weight", 0.0))
            except (TypeError, ValueError):
                return 0.0
        return g.weight_for(node.attribute)

    @staticmethod
    def _cross_instance(ev: GroupEvaluation) -> None:
        by_parent: Dict[str, List[LeafResult]] = {}
        for lf in ev.attribute_leaves():
            parts = attr_path_parts(lf.attribute)
            if len(parts) > 1 and lf.passed and not lf.null_case and lf.equal_instance_pairs:
                by_parent.setdefault(parts[0], []).append(lf)
        for parent, leaves in by_parent.items():
            if len(leaves) < 2:
                continue
            sets = [lf.equal_instance_pairs for lf in leaves]
            if any(("", "") in s or any(not a or not b for a, b in s) for s in sets):
                continue                                  # instance ids unknown (report values)
            common = set.intersection(*sets)
            if not common:
                ev.cross_instance_attrs.append(parent)


def operator_status(op: str) -> str:
    return OPERATOR_DOCS.get(op, {}).get("status", APPROXIMATION)
