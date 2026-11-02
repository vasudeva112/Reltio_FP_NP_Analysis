"""
Null-semantics probe: measures - instead of assuming - how Reltio treats operands whose values
are missing on both sides (and on one side) in THIS tenant.

Reltio documents exact / fuzzy as false when a value is missing; exactOrNull as true for one-
or both-sided nulls; exactOrAllNull as true only when both are missing. Tenants have been seen to
return potential matches whose exact operands were null on both records. The probe re-evaluates
every Potential Match Report row that has at least one null-affected operand under each
BOTH_NULL_EXACT_MODE (fail / pass / exclude) and reports, per rule:

  * how many of those Reltio-returned rows each mode reproduces
  * the mean absolute difference to Reltio's own relevance (relevance_based rules, when the
    report carries ReltioRelevance)

The mode with the best agreement is a recommendation backed by evidence counts, never a silent
switch: set BOTH_NULL_EXACT_MODE (or choose it in the UI) to apply it. Agreement on positives only
is not verification - confirm a sample with the read-only `verify` mode.
"""

from collections import defaultdict
from typing import Dict, List, Optional, Sequence

from ..attribute_extractor import EntityRecord
from ..comparators.base import BOTH_NULL_MODES, EngineContext
from ..operators import BOTH_NULL, ONE_SIDED_NULL
from ..rule_config_parser import MatchGroup
from ..rule_evaluator import RuleEngine


def _ctx_like(ctx: EngineContext, mode: str) -> EngineContext:
    return EngineContext(case_insensitive=ctx.case_insensitive, treat_blank_as_null=ctx.treat_blank_as_null,
                         noise_dictionaries=ctx.noise_dictionaries, name_dictionary=ctx.name_dictionary,
                         regexp_rules=ctx.regexp_rules, relevance_constraint_mode=ctx.relevance_constraint_mode,
                         relevance_nested_aggregation=ctx.relevance_nested_aggregation,
                         double_metaphone_max_length=ctx.double_metaphone_max_length, both_null_exact_mode=mode)


def probe(pairs: Sequence[tuple], groups: Sequence[MatchGroup], base_ctx: EngineContext) -> List[dict]:
    """pairs: (MatchGroup, left EntityRecord, right EntityRecord, reltio_relevance or None) for rows Reltio
    returned. Returns one row per rule and mode, plus a recommendation row per rule."""
    engines = {m: RuleEngine(list(groups), _ctx_like(base_ctx, m)) for m in BOTH_NULL_MODES}
    stats: Dict[str, Dict[str, dict]] = defaultdict(lambda: {m: {"rows": 0, "reproduced": 0, "abs_err": [],
                                                                  "both_null_ops": 0, "one_null_ops": 0}
                                                              for m in BOTH_NULL_MODES})
    labels = {}
    for g, left, right, reltio_rel in pairs:
        ev0 = engines["fail"].evaluate(g, left, right)
        leaves = [l for l in ev0.attribute_leaves() if l.op in ("exact", "fuzzy", "exactOrNull", "exactOrAllNull")]
        both = sum(1 for l in leaves if l.null_case == BOTH_NULL and l.op in ("exact", "fuzzy"))
        one = sum(1 for l in leaves if l.null_case == ONE_SIDED_NULL)
        if not any(l.null_case for l in leaves):
            continue
        labels[g.uri] = g.label
        for m, eng in engines.items():
            ev = ev0 if m == "fail" else eng.evaluate(g, left, right)
            st = stats[g.uri][m]
            st["rows"] += 1
            st["both_null_ops"] += both
            st["one_null_ops"] += one
            st["reproduced"] += 1 if ev.matched else 0
            if reltio_rel is not None and ev.relevance is not None:
                st["abs_err"].append(abs(reltio_rel - ev.relevance))
    out: List[dict] = []
    for uri, per_mode in stats.items():
        best = None
        for m in BOTH_NULL_MODES:
            st = per_mode[m]
            mae = sum(st["abs_err"]) / len(st["abs_err"]) if st["abs_err"] else None
            rate = st["reproduced"] / st["rows"] if st["rows"] else None
            out.append({"MatchRule": labels[uri], "MatchRuleURI": uri, "BothNullExactMode": m,
                        "RowsWithNullOperands": st["rows"], "BothNullExactOperands": st["both_null_ops"],
                        "OneSidedNullOperands": st["one_null_ops"], "ReltioRowsReproduced": st["reproduced"],
                        "ReproductionRate": None if rate is None else round(rate, 4),
                        "RowsWithReltioRelevance": len(st["abs_err"]),
                        "MeanAbsRelevanceError": None if mae is None else round(mae, 4)})
            key = (rate or 0, -(mae if mae is not None else 1e9))
            if best is None or key > best[0]:
                best = (key, m, rate, mae)
        both_ops = per_mode["fail"]["both_null_ops"]
        if both_ops == 0:
            rec = "No both-null exact/fuzzy operands in Reltio's results for this rule: no evidence either way."
        else:
            rec = (f"Mode '{best[1]}' agrees best with Reltio "
                   f"(reproduces {best[2]:.1%} of {per_mode['fail']['rows']} null-affected rows"
                   + (f", relevance error {best[3]:.4f}" if best[3] is not None else "") + ").")
        out.append({"MatchRule": labels[uri], "MatchRuleURI": uri, "BothNullExactMode": "RECOMMENDATION",
                    "Recommendation": rec})
    return out
