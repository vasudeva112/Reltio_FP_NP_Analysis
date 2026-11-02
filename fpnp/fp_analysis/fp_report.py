"""
False Positive report (Excel + CSV detail).

  Sheet 1  FP Summary            totals, classification, rule-wise / attribute-wise distribution,
                                 overall score distribution, high / medium / low similarity groups
  Sheet 2  FP Pair Details       one row per potential-match pair + rule
  Sheet 3  FP Attribute Details  one row per pair x rule operand
  Sheet 4  Scoring Model         the scoring configuration used (reproducibility)
  Sheet 5  Null Semantics        (optional) both-null calibration against Reltio's own results
"""

import json
import os
import time
from typing import List, Optional, Sequence

from ..reltio_engine.excel_writer import ReportWorkbook, write_csv
from ..reltio_engine.metrics import FP_SEVERITY, fp_metrics, pct
from .fp_analyzer import FPResult

PAIR_COLUMNS = ["PairID", "EntityA", "EntityB", "EntityType", "MatchRule", "MatchRuleURI", "RuleType",
                "ReltioMatchScore", "ReltioMatchAction", "EstimatedSimilarityScore", "SimilarityGroup",
                "AnalyticalRuleEvaluation", "PythonRelevance", "PythonAction", "FPClassification",
                "ClassificationBasis", "Explanation", "MatchedAttributes", "UnmatchedAttributes",
                "VariantAttributes", "NullAttributes", "NullSatisfiedConditions", "Conflicts", "TopContributors",
                "RiskIndicators", "RuleFidelity", "RuleSource", "ValueSource", "StewardDecision",
                "RuleExplanation", "SourceCrosswalks", "MatchCrosswalks", "SourceFile", "SourceRow"]
ATTR_COLUMNS = ["PairID", "EntityA", "EntityB", "MatchRule", "Attribute", "AttributeURI", "Role", "Operator",
                "Comparator", "ComparatorType", "ComparatorParams", "EntityA_Value", "EntityB_Value",
                "EntityA_Normalized", "EntityB_Normalized", "AttributeScore", "ScoreBand", "Weight", "Contribution",
                "InOverallScore", "ComparisonResult", "OperatorPassed", "ComparatorRelevance",
                "AnalyticalSimilarity", "NullCase", "MultiValue", "Nested", "Conflict", "Cleansing",
                "FidelityStatus", "Explanation"]

SCORE_NOTE = ("Estimated similarity is an analytical 0-100 score computed from the rule's attributes with the "
              "configured comparators. It is NOT Reltio's internal match score; Reltio's score is shown separately "
              "when the report carries it (ReltioMatchScore).")


def _share(n, total):
    return pct(n, total)


def summary_sections(res: FPResult, null_probe: Optional[List[dict]] = None) -> List[tuple]:
    m = fp_metrics(res.pairs, res.attributes, res.stats)
    cfg = res.config
    total = m["rows"] or 1
    overview = [
        {"Metric": "Total potential match rows (pair + rule)", "Value": m["total_rows"]},
        {"Metric": "Unique potential match pairs", "Value": m["unique_pairs"]},
        {"Metric": "Rows analysed", "Value": m["evaluated"]},
        {"Metric": "Rows not evaluated (rule / entity type not configured)", "Value": m["not_evaluated"]},
        {"Metric": "Rows skipped (same entity on both sides)", "Value": m["same_entity_rows"]},
        {"Metric": "Average estimated similarity", "Value": m["score_stats"]["Avg"]},
        {"Metric": "Median estimated similarity", "Value": m["score_stats"]["Median"]},
        {"Metric": "Rows whose match Python rule evaluation reproduces", "Value": m["reproduced"],
         "Share": _share(m["reproduced"], m["evaluated"])},
        {"Metric": "Rows carrying Reltio's own match score", "Value": m["reltio_scored"]},
        {"Metric": "Report entities not found in entity JSON", "Value": m["entity_missing_from_export"]},
        {"Metric": "Value source", "Value": "; ".join(f"{k}: {v}" for k, v in m["value_source"].items())},
        {"Metric": "Analysis seconds", "Value": res.seconds},
    ]
    cls_rows = [{"FPClassification": c, "Rows": m["classification_rows"][c],
                 "Share": _share(m["classification_rows"][c], total),
                 "UniquePairs": m["classification_pairs"][c]} for c in FP_SEVERITY if m["classification_rows"][c]]
    groups = [{"SimilarityGroup": g, "Threshold": t, "Rows": m["groups"][g], "Share": _share(m["groups"][g], m["evaluated"])}
              for g, t in (("High", f">= {cfg.similarity_groups.get('high', 85):g}"),
                           ("Medium", f">= {cfg.similarity_groups.get('medium', 60):g}"),
                           ("Low", f"< {cfg.similarity_groups.get('medium', 60):g}"),
                           ("Not scored", "no comparable values"))]
    return [
        ("Overview", overview, SCORE_NOTE),
        ("FP classification", cls_rows,
         "Classifications other than 'Confirmed ...' are analytical hypotheses for steward review."),
        ("High / medium / low similarity groups", groups),
        ("Overall score distribution", m["score_bins"]),
        ("Rule-wise distribution", m["rules"]),
        ("Attribute-wise distribution (positive match conditions)", m["attributes"]),
        ("Most frequent risk indicators", [{"RiskIndicator": k, "Rows": v} for k, v in m["risk_indicators"][:15]]),
        ("Attributes most often disagreeing in returned pairs",
         [{"Attribute": k, "Rows": v} for k, v in m["unmatched_attributes"][:15]]),
    ] + ([("Null semantics calibration (both-null exact/fuzzy)", null_probe,
           "How each BOTH_NULL_EXACT_MODE reproduces Reltio's returned pairs - evidence for the setting, "
           "not verification.")] if null_probe else [])


def write_fp_report(res: FPResult, out_dir: str, row_cap: int = 200_000, null_probe: Optional[List[dict]] = None,
                    name: str = "Reltio_FP_Analysis_Report.xlsx") -> dict:
    os.makedirs(os.path.join(out_dir, "detail"), exist_ok=True)
    det = os.path.join(out_dir, "detail")
    files = {
        "fp_pairs_csv": write_csv(os.path.join(det, "fp_pair_details.csv"), res.pairs, PAIR_COLUMNS),
        "fp_attributes_csv": write_csv(os.path.join(det, "fp_attribute_details.csv"), res.attributes, ATTR_COLUMNS),
    }
    wb = ReportWorkbook(os.path.join(out_dir, name), row_cap)
    wb.add_sections("FP Summary", "Reltio False Positive Analysis - Summary", summary_sections(res, null_probe),
                    subtitle=f"Generated {time.strftime('%Y-%m-%d %H:%M')} | rules analysed: {len(res.rules)}")
    wb.add_table("FP Pair Details", "Potential match pairs - analytical evaluation", res.pairs, PAIR_COLUMNS,
                 files["fp_pairs_csv"], SCORE_NOTE)
    wb.add_table("FP Attribute Details", "Attribute-wise comparison per pair and rule", res.attributes, ATTR_COLUMNS,
                 files["fp_attributes_csv"],
                 "Only attributes used by the applicable match rule are compared. Normalized = value as the "
                 "configured comparator sees it.")
    cfg_rows = [{"Setting": k, "Value": json.dumps(v) if isinstance(v, (dict, list)) else v}
                for k, v in res.config.to_dict().items()]
    wb.add_table("Scoring Model", "Scoring configuration used", cfg_rows, ["Setting", "Value"])
    if null_probe:
        files["null_probe_csv"] = write_csv(os.path.join(det, "null_semantics_probe.csv"), null_probe,
                                            list(dict.fromkeys(k for r in null_probe for k in r)))
    files["fp_workbook"] = wb.save()
    return files
