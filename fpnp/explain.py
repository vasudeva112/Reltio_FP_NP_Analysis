"""Turns a GroupEvaluation into report columns, risk indicators and investigation hints."""

from typing import Dict, List, Optional, Tuple

from .comparators.base import DOCUMENTED
from .operators import ONE_SIDED_NULL
from .rule_config_parser import CONSTRAINT_OPS, MatchGroup, short_attr
from .rule_evaluator import GroupEvaluation


def per_attribute_results(ev: GroupEvaluation) -> str:
    parts = []
    for t in ev.trace:
        lf = t.leaf
        if not lf:
            continue
        tag = "NOT:" if t.under_not else ""
        res = "PASS" if lf.passed else "FAIL"
        if lf.is_constraint:
            parts.append(f"{tag}{lf.op}({short_attr(lf.attribute)})={res}")
            continue
        rel = f" r={lf.relevance:.2f}" if ev.relevance is not None else ""
        why = lf.null_case or (f"{lf.matched_values[0]}~{lf.matched_values[1]}" if any(lf.matched_values) else "")
        parts.append(f"{tag}{short_attr(lf.attribute)}[{lf.op}/{lf.comparator}]={res}{rel}"
                     + (f" ({why})" if why else ""))
    return "; ".join(parts)


def value_columns(ev: GroupEvaluation, prefix_l: str = "L_", prefix_r: str = "R_") -> Dict[str, str]:
    out: Dict[str, str] = {}
    for t in ev.trace:
        lf = t.leaf
        if not lf or not lf.attribute or lf.op == "multi":
            continue
        a = short_attr(lf.attribute)
        out.setdefault(f"{prefix_l}{a}", lf.left)
        out.setdefault(f"{prefix_r}{a}", lf.right)
    return out


def total_positive_weight(g: MatchGroup) -> float:
    w = 0.0
    for n in g.root.walk():
        if n.kind == "op" and n.op not in CONSTRAINT_OPS:
            w += g.weight_for(n.attribute)
    return w or 1.0


def risk_indicators(ev: GroupEvaluation, g: MatchGroup, high_freq: Dict[Tuple[str, str], int],
                    freq_threshold: int, low_margin: float, reltio_matched: bool,
                    attr_avg_freq: Optional[Dict[str, float]] = None, freq_factor: float = 5.0,
                    reltio_relevance: Optional[float] = None) -> List[Tuple[str, str]]:
    """List of (indicator, detail)."""
    ind: List[Tuple[str, str]] = []
    leaves = ev.attribute_leaves()
    unmet = [l for l in ev.leaves() if l.is_constraint and not l.passed]
    if reltio_matched and unmet:
        ind.append(("CONSTRAINT_VIOLATED", "; ".join(
            f"{l.op}({short_attr(l.attribute)}) L={l.left} R={l.right}" for l in unmet)))
    failed = [l for l in leaves if not l.passed]
    if failed and (ev.matched or reltio_matched):
        tw = total_positive_weight(g)
        det = ", ".join(f"{short_attr(l.attribute)}({l.left} vs {l.right}"
                        + (f", weight share {g.weight_for(l.attribute) / tw:.0%}" if g.is_relevance else "")
                        + ")" for l in failed)
        ind.append(("FAILED_OPERAND_STILL_MATCHED", det))
    diff = [l for l in leaves if l.values_differ]
    if diff:
        ind.append(("POPULATED_VALUES_DIFFER",
                    ", ".join(f"{short_attr(l.attribute)}: {l.left} vs {l.right}" for l in diff)))
    nulls = [l for l in leaves if l.passed and l.null_case]
    if nulls:
        ind.append(("NULL_CONTRIBUTION", ", ".join(f"{short_attr(l.attribute)} {l.op} {l.null_case}" for l in nulls)))
    one = [l for l in nulls if l.null_case == ONE_SIDED_NULL]
    if one:
        ind.append(("ONE_SIDED_NULL", ", ".join(f"{short_attr(l.attribute)}: {l.left} vs {l.right}" for l in one)))
    perm = [l for l in leaves if l.permissive_hit]
    if perm:
        ind.append(("PERMISSIVE_COMPARATOR", ", ".join(
            f"{short_attr(l.attribute)} {l.matched_values[0]}~{l.matched_values[1]} via {l.comparator}"
            for l in perm)))
    if ev.cross_instance_attrs:
        ind.append(("CROSS_INSTANCE_NESTED_MATCH", ", ".join(ev.cross_instance_attrs)))
    mp = [l for l in leaves if l.multi_partial]
    if mp:
        ind.append(("MULTI_VALUE_PARTIAL", ", ".join(f"{short_attr(l.attribute)}: {l.left} vs {l.right}" for l in mp)))
    match_floor = min((t.lo for t in g.thresholds), default=None)     # match / no-match boundary
    if ev.relevance is not None and ev.matched and match_floor is not None \
            and ev.relevance - match_floor < low_margin:
        ind.append(("LOW_MARGIN", f"relevance {ev.relevance:.4f} vs lowest threshold {match_floor}"))
    hf = []
    avg = attr_avg_freq or {}
    for l in leaves:
        if l.passed and not l.null_case and l.matched_values[0]:
            a = short_attr(l.attribute)
            n = high_freq.get((a, l.matched_values[0].strip().lower()), 0)
            # relative to the attribute's own cardinality, so categorical values (flags, types) are not flagged
            if avg.get(a, 1.0) != float("inf") and n >= max(freq_threshold, freq_factor * avg.get(a, 1.0)):
                hf.append(f"{a}='{l.matched_values[0]}' shared by {n} entities "
                          f"(attribute average {avg.get(a, 1.0):.1f})")
    if hf:
        ind.append(("HIGH_FREQUENCY_VALUE", "; ".join(hf)))
    if reltio_matched and not ev.matched:
        why = ev.not_applicable_reason or (
            f"relevance {ev.relevance:.4f} outside actionThresholds" if ev.relevance is not None
            else "boolean logic not satisfied")
        ind.append(("PYTHON_DISAGREES", why))
    if reltio_relevance is not None and ev.relevance is not None and abs(reltio_relevance - ev.relevance) > 0.005:
        ind.append(("RELTIO_RELEVANCE_DIFFERS", f"Reltio {reltio_relevance:.4f} vs Python {ev.relevance:.4f}"))
    if ev.decisive_status() != DOCUMENTED:
        ind.append(("APPROXIMATION_DEPENDENT", ev.decisive_status()))
    return ind


def suggestions(ind: List[Tuple[str, str]], ev: GroupEvaluation, g: MatchGroup) -> List[str]:
    out = []
    names = {k for k, _ in ind}
    if "CONSTRAINT_VIOLATED" in names:
        out.append("Reltio returned a pair that fails one of the group's own constraints (equals/in/notEquals): "
                   "verify on the tenant whether the constraint is enforced for this relevance_based group.")
    if "FAILED_OPERAND_STILL_MATCHED" in names and g.is_relevance:
        out.append(f"Failed attributes carry too little weight to stop a match: review weights or raise the "
                   f"lowest actionThreshold ({min((t.lo for t in g.thresholds), default='n/a')}).")
    if "ONE_SIDED_NULL" in names:
        out.append("exactOrNull lets a missing value match a populated one: check whether exactOrAllNull is "
                   "intended for these attributes.")
    if "HIGH_FREQUENCY_VALUE" in names:
        out.append("A matched value is shared by many entities: check whether it is a placeholder that "
                   "belongs in nullValues.")
    if "CROSS_INSTANCE_NESTED_MATCH" in names:
        out.append("Nested sub-attributes matched on different instances: check on the tenant whether Type and "
                   "Value should be compared together.")
    if "PERMISSIVE_COMPARATOR" in names:
        out.append("Different values were accepted by a phonetic/fuzzy comparator: review the comparator class "
                   "or its threshold for these attributes.")
    if "PYTHON_DISAGREES" in names:
        out.append("Python logic does not reproduce Reltio's result: verify with _verifyMatches before "
                   "drawing a rule conclusion.")
    return out
