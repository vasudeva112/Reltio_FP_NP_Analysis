"""
False Negative report (Excel + CSV detail).

  Sheet 1  FN Summary           entities processed, candidate pairs, high / medium / low / review counts,
                                rule-wise candidate counts, missed-match reasons, score distribution,
                                exclusions (already merged / matched)
  Sheet 2  FN Pair Details      one row per reported candidate pair (best explaining rule)
  Sheet 3  FN Attribute Details one row per pair x rule operand (OV values)
  Sheet 4  Blocking Analysis    per blocking strategy / key: entities, blocks, candidate pairs, matching
                                candidates; plus the largest blocks
  Sheet 5  Scoring Model        scoring configuration used
"""

import json
import os
import time
from typing import List

from ..reltio_engine.excel_writer import ReportWorkbook, write_csv
from ..reltio_engine.metrics import FN_SEVERITY, fn_metrics, pct
from .fn_analyzer import FNResult

PAIR_COLUMNS = ["PairID", "EntityA", "EntityB", "EntityType", "ApplicableMatchRule", "ApplicableMatchRuleURI",
                "RuleType", "RulesSatisfied", "OverallSimilarityScore", "SimilarityGroup", "RuleEvaluation",
                "PythonRelevance", "FNConfidence", "MissedMatchReasonCode", "ReasonForPotentialMissedMatch",
                "RecommendedAction", "ReltioTokenStatus", "BlockingStrategies", "MatchedAttributes",
                "UnmatchedAttributes", "NullAttributes", "Conflicts", "TopContributors", "RuleFidelity",
                "EntityA_Crosswalks", "EntityB_Crosswalks", "EntityA_Updated", "EntityB_Updated", "EntityA_File",
                "EntityB_File", "StewardDecision", "RuleExplanation"]
ATTR_COLUMNS = ["PairID", "EntityA", "EntityB", "MatchRule", "Attribute", "AttributeURI", "Role", "Operator",
                "Comparator", "ComparatorType", "ComparatorParams", "EntityA_Value", "EntityB_Value",
                "EntityA_Normalized", "EntityB_Normalized", "AttributeScore", "ScoreBand", "Weight", "Contribution",
                "InOverallScore", "ComparisonResult", "OperatorPassed", "ComparatorRelevance",
                "AnalyticalSimilarity", "NullCase", "MultiValue", "Nested", "Conflict", "Cleansing",
                "FidelityStatus", "Explanation"]
BLOCK_COLUMNS = ["BlockingKey", "BlockingStrategy", "StrategyName", "EntityType", "NumberOfEntities", "Blocks",
                 "BlocksWithPairs", "LargestBlock", "OversizedBlocksSkipped", "EntitiesInSkippedBlocks", "SkippedLowSelectivity", "ProjectedPairs",
                 "CandidatePairsGenerated", "PairsFoundOnlyByThisStrategy", "MatchingCandidates",
                 "ReportedFNCandidates", "Description"]

NOTE = ("Values are OV (ov=true) golden values only. Overall similarity is an analytical estimate, not Reltio's "
        "match score. Candidates are hypotheses: only steward-confirmed pairs are 'Confirmed False Negative'.")


def summary_sections(res: FNResult) -> List[tuple]:
    m = fn_metrics(res.pairs, res.attributes, res.blocking, res.stats)
    st = res.stats
    ev = st.get("pairs_evaluated", 0)
    overview = [
        {"Metric": "Total entities processed", "Value": m["entities"]},
        {"Metric": "Entities by type", "Value": "; ".join(f"{k}: {v}" for k, v in (st.get("entity_types") or {}).items())},
        {"Metric": "Entities read in other (unconfigured) types",
         "Value": sum((st.get("skipped_other_types") or {}).values())},
        {"Metric": "Duplicate entity URIs across exports (first kept)", "Value": st.get("duplicate_uris", 0)},
        {"Metric": "Non-OV values ignored (rule attributes)", "Value": st.get("non_ov_values_ignored", 0)},
        {"Metric": "Blocking strategies used", "Value": st.get("strategies", 0)},
        {"Metric": "Total candidate pairs generated", "Value": m["candidate_pairs"]},
        {"Metric": "Candidate pairs evaluated", "Value": ev},
        {"Metric": "Total high-confidence candidates", "Value": m["high"]},
        {"Metric": "Total medium-confidence candidates", "Value": m["medium"]},
        {"Metric": "Total low-confidence candidates", "Value": m["low"]},
        {"Metric": "Requires manual review", "Value": m["review"]},
        {"Metric": "Confirmed false negatives (steward evidence)", "Value": m["confirmed"]},
        {"Metric": "Processing seconds (total / blocking)",
         "Value": f"{st.get('seconds')} / {st.get('blocking_seconds')}"},
        {"Metric": "Peak Python heap (MB)", "Value": st.get("peak_memory_mb_python_heap")},
    ]
    excl = [{"Exclusion": k, "Pairs": v} for k, v in (st.get("excluded") or {}).items()]
    excl += [{"Exclusion": f"not reported: {k}", "Pairs": v} for k, v in (st.get("not_reported") or {}).items()]
    conf = [{"FNConfidence": c, "Pairs": m["confidence"][c], "Share": pct(m["confidence"][c], len(res.pairs))}
            for c in FN_SEVERITY if m["confidence"][c]]
    return [
        ("Overview", overview, NOTE),
        ("Confidence classification", conf),
        ("Rule-wise candidate counts", m["rules"]),
        ("Reasons for potential missed matches", [{"ReasonCode": k, "Pairs": v} for k, v in m["reasons"]]),
        ("Overall similarity distribution (reported candidates)", m["score_bins"]),
        ("Pairs excluded before / after evaluation", excl,
         "Already merged (shared crosswalk / merge map) and already matched pairs are not missed matches."),
        ("Attribute-wise distribution (reported candidates)", m["attributes"]),
    ]


def write_fn_report(res: FNResult, out_dir: str, row_cap: int = 200_000,
                    name: str = "Reltio_FN_Analysis_Report.xlsx") -> dict:
    det = os.path.join(out_dir, "detail")
    os.makedirs(det, exist_ok=True)
    files = {
        "fn_pairs_csv": write_csv(os.path.join(det, "fn_pair_details.csv"), res.pairs, PAIR_COLUMNS),
        "fn_attributes_csv": write_csv(os.path.join(det, "fn_attribute_details.csv"), res.attributes, ATTR_COLUMNS),
        "fn_blocking_csv": write_csv(os.path.join(det, "fn_blocking_analysis.csv"), res.blocking, BLOCK_COLUMNS),
    }
    wb = ReportWorkbook(os.path.join(out_dir, name), row_cap)
    wb.add_sections("FN Summary", "Reltio False Negative Analysis - Summary", summary_sections(res),
                    subtitle=f"Generated {time.strftime('%Y-%m-%d %H:%M')} | rules analysed: {len(res.rules)}")
    wb.add_table("FN Pair Details", "Candidate missed matches", res.pairs, PAIR_COLUMNS, files["fn_pairs_csv"], NOTE)
    wb.add_table("FN Attribute Details", "Attribute-wise comparison (OV values)", res.attributes, ATTR_COLUMNS,
                 files["fn_attributes_csv"])
    wb.add_sections("Blocking Analysis", "Blocking analysis", [
        ("Blocking strategies", [{k: r.get(k) for k in BLOCK_COLUMNS} for r in res.blocking],
         "Candidate pairs are the union of all strategies. MatchingCandidates = pairs satisfying at least one rule "
         "in Python; ReportedFNCandidates = pairs in FN Pair Details."),
        ("Largest blocks", res.largest_blocks, "Blocks above the size limit are skipped (and counted) to avoid "
                                               "quadratic comparison of very common values."),
    ])
    cfg_rows = [{"Setting": k, "Value": json.dumps(v) if isinstance(v, (dict, list)) else v}
                for k, v in res.config.to_dict().items()]
    wb.add_table("Scoring Model", "Scoring configuration used", cfg_rows, ["Setting", "Value"])
    files["fn_workbook"] = wb.save()
    return files
