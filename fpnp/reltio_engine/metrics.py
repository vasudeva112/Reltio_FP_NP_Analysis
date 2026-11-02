"""
Aggregate metrics for the FP and FN results. One implementation feeds the Excel summary
sheets, the UI and the client metrics report, so the numbers always agree.
"""

import statistics
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence

SCORE_BINS = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]

FP_SEVERITY = ["Confirmed False Positive", "Potential False Positive", "Requires Manual Review",
               "Likely Genuine Duplicate", "Confirmed Genuine Duplicate", "Not Evaluated"]
FN_SEVERITY = ["Confirmed False Negative", "High-confidence potential false negative",
               "Medium-confidence potential false negative", "Requires manual review", "Low-confidence candidate",
               "Excluded"]


def _num(v) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def pct(part: float, whole: float) -> Optional[float]:
    return round(part / whole, 4) if whole else None


def score_bins(values: Iterable[Optional[float]]) -> List[dict]:
    vals = list(values)
    n = len(vals)
    out = []
    for lo, hi in SCORE_BINS:
        c = sum(1 for v in vals if v is not None and lo <= v < hi)
        out.append({"ScoreRange": f"{lo}-{min(hi, 100)}", "Pairs": c, "Share": pct(c, n)})
    c = sum(1 for v in vals if v is None)
    if c:
        out.append({"ScoreRange": "Not scored", "Pairs": c, "Share": pct(c, n)})
    return out


def _stats(vals: Sequence[float]) -> dict:
    if not vals:
        return {"Avg": None, "Median": None, "Min": None, "Max": None}
    return {"Avg": round(statistics.fmean(vals), 1), "Median": round(statistics.median(vals), 1),
            "Min": round(min(vals), 1), "Max": round(max(vals), 1)}


def attribute_distribution(attrs: Iterable[dict], key_rule: bool = False) -> List[dict]:
    """Per attribute (positive match conditions only): how comparisons ended."""
    agg: Dict[tuple, Counter] = defaultdict(Counter)
    scores: Dict[tuple, List[float]] = defaultdict(list)
    meta: Dict[tuple, dict] = {}
    for a in attrs:
        if a.get("Role") != "Match condition":
            continue
        k = (a["Attribute"], a["MatchRule"]) if key_rule else (a["Attribute"],)
        m = meta.setdefault(k, {"Comparators": set(), "Rules": set(), "URI": a.get("AttributeURI", "")})
        m["Comparators"].add(a.get("Comparator", ""))
        m["Rules"].add(a.get("MatchRule", ""))
        agg[k][a.get("ComparisonResult", "")] += 1
        agg[k]["_total"] += 1
        if a.get("Conflict"):
            agg[k]["_conflict"] += 1
        s = _num(a.get("AttributeScore"))
        if s is not None:
            scores[k].append(s)
    out = []
    for k, c in sorted(agg.items(), key=lambda kv: -kv[1]["_total"]):
        t = c["_total"]
        disagree = c["NO_MATCH"] + c["PARTIAL"]
        row = {"Attribute": k[0]}
        if key_rule:
            row["MatchRule"] = k[1]
        row.update({
            "AttributeURI": meta[k]["URI"],
            "Comparators": ", ".join(sorted(x for x in meta[k]["Comparators"] if x)),
            "Rules": len(meta[k]["Rules"]), "Comparisons": t,
            "Exact": c["EXACT"], "AcceptedVariant": c["VARIANT_MATCH"], "PartialNearMiss": c["PARTIAL"],
            "NoMatch": c["NO_MATCH"], "BothNull": c["BOTH_NULL"], "OneNull": c["ONE_NULL"],
            "Unsupported": c["UNSUPPORTED"], "Conflicts": c["_conflict"],
            "MatchRate": pct(c["EXACT"] + c["VARIANT_MATCH"], t), "DisagreeRate": pct(disagree, t),
            "NullRate": pct(c["BOTH_NULL"] + c["ONE_NULL"], t),
            "AvgAttributeScore": _stats(scores[k])["Avg"],
        })
        out.append(row)
    return out


def _pair_level(rows: Sequence[dict], cls_key: str, severity: List[str]) -> Dict[str, str]:
    order = {c: i for i, c in enumerate(severity)}
    best: Dict[str, str] = {}
    for r in rows:
        k, c = r.get("PairID", ""), r.get(cls_key, "")
        if k not in best or order.get(c, 99) < order.get(best[k], 99):
            best[k] = c
    return best


# ----------------------------------------------------------------------
def fp_metrics(pairs: Sequence[dict], attrs: Sequence[dict], stats: Optional[dict] = None) -> dict:
    stats = dict(stats or {})
    evaluated = [r for r in pairs if r.get("FPClassification") != "Not Evaluated"]
    scores = [_num(r.get("EstimatedSimilarityScore")) for r in evaluated]
    pair_cls = _pair_level(pairs, "FPClassification", FP_SEVERITY)
    rule_rows = []
    by_rule: Dict[str, List[dict]] = defaultdict(list)
    for r in evaluated:
        by_rule[r.get("MatchRule", "")].append(r)
    for rule, rs in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        sc = [s for s in (_num(x.get("EstimatedSimilarityScore")) for x in rs) if s is not None]
        cls = Counter(x["FPClassification"] for x in rs)
        grp = Counter(x.get("SimilarityGroup", "") for x in rs)
        rep = sum(1 for x in rs if x.get("AnalyticalRuleEvaluation") == "MATCH")
        rel = [v for v in (_num(x.get("ReltioMatchScore")) for x in rs) if v is not None]
        row = {"MatchRule": rule, "RuleType": rs[0].get("RuleType", ""), "Rows": len(rs),
               "UniquePairs": len({x["PairID"] for x in rs}), **{f"Similarity{k}": v for k, v in _stats(sc).items()},
               "PythonReproduces": rep, "ReproductionRate": pct(rep, len(rs)),
               "AvgReltioScore": round(statistics.fmean(rel), 4) if rel else None,
               "High": grp["High"], "Medium": grp["Medium"], "Low": grp["Low"]}
        for c in FP_SEVERITY[:-1]:
            row[c] = cls[c]
        row["PotentialFPRate"] = pct(cls["Potential False Positive"] + cls["Confirmed False Positive"], len(rs))
        rule_rows.append(row)
    risk = Counter()
    for r in evaluated:
        for k in (r.get("RiskIndicators") or "").split(", "):
            if k:
                risk[k] += 1
    unmatched = Counter()
    for r in evaluated:
        for k in (r.get("UnmatchedAttributes") or "").split(", "):
            if k:
                unmatched[k] += 1
    return {
        "total_rows": stats.get("report_rows", len(pairs)),
        "same_entity_rows": stats.get("same_entity_rows", 0),
        "rows": len(pairs), "evaluated": len(evaluated),
        "not_evaluated": len(pairs) - len(evaluated),
        "unique_pairs": len(pair_cls),
        "classification_rows": Counter(r["FPClassification"] for r in pairs),
        "classification_pairs": Counter(pair_cls.values()),
        "groups": Counter(r.get("SimilarityGroup", "") for r in evaluated),
        "score_stats": _stats([s for s in scores if s is not None]),
        "score_bins": score_bins(scores),
        "rules": rule_rows,
        "attributes": attribute_distribution(attrs),
        "reproduced": sum(1 for r in evaluated if r.get("AnalyticalRuleEvaluation") == "MATCH"),
        "reltio_scored": sum(1 for r in evaluated if _num(r.get("ReltioMatchScore")) is not None),
        "value_source": Counter(r.get("ValueSource", "") for r in evaluated),
        "risk_indicators": risk.most_common(),
        "unmatched_attributes": unmatched.most_common(),
        "entity_missing_from_export": stats.get("entity_missing_from_export", 0),
    }


def fn_metrics(pairs: Sequence[dict], attrs: Sequence[dict], blocking: Sequence[dict], stats: dict) -> dict:
    conf = Counter(r.get("FNConfidence", "") for r in pairs)
    rules = defaultdict(Counter)
    for r in pairs:
        rules[r.get("ApplicableMatchRule", "")][r.get("FNConfidence", "")] += 1
        rules[r.get("ApplicableMatchRule", "")]["_total"] += 1
        if r.get("RuleEvaluation") == "MATCH":
            rules[r.get("ApplicableMatchRule", "")]["_pass"] += 1
    rule_rows = []
    for rule, c in sorted(rules.items(), key=lambda kv: -kv[1]["_total"]):
        row = {"MatchRule": rule, "Candidates": c["_total"], "SatisfyRule": c["_pass"]}
        for k in FN_SEVERITY:
            row[k] = c[k]
        rule_rows.append(row)
    reasons = Counter(r.get("MissedMatchReasonCode", "") for r in pairs if r.get("MissedMatchReasonCode"))
    return {
        "entities": stats.get("entities_loaded", 0), "entity_types": stats.get("entity_types", {}),
        "candidate_pairs": stats.get("candidate_pairs", 0), "pairs_evaluated": stats.get("pairs_evaluated", 0),
        "reported_pairs": len(pairs), "confidence": conf,
        "high": conf["High-confidence potential false negative"],
        "medium": conf["Medium-confidence potential false negative"],
        "low": conf["Low-confidence candidate"], "review": conf["Requires manual review"],
        "confirmed": conf["Confirmed False Negative"],
        "rules": rule_rows, "reasons": reasons.most_common(),
        "score_bins": score_bins(_num(r.get("OverallSimilarityScore")) for r in pairs),
        "score_stats": _stats([s for s in (_num(r.get("OverallSimilarityScore")) for r in pairs) if s is not None]),
        "attributes": attribute_distribution(attrs),
        "blocking": list(blocking),
        "excluded": stats.get("excluded", {}),
        "seconds": stats.get("seconds"),
    }
