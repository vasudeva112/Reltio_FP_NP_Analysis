"""Reltio FP / FN modules on the 14-scenario validation dataset, reports, config tools, scoring formula."""

import csv
import json
import os

import openpyxl
import pytest

from fpnp import reltio_pipeline as rp
from fpnp import reltio_testdata as td
from fpnp.client_metrics_report import write_client_reports
from fpnp.comparators.base import EngineContext
from fpnp.attribute_extractor import build_record
from fpnp.fn_analysis.blocking_engine import derive_strategies, parse_custom_key
from fpnp.fp_analysis.potential_match_parser import INFER_RULE, read_potential_matches
from fpnp.reltio_engine.match_rule_parser import (ConfigError, attribute_catalog, compare_configs, load_groups,
                                                  load_rule_config, rule_catalog)
from fpnp.reltio_engine.scoring_engine import PairScorer
from fpnp.rule_evaluator import RuleEngine
from fpnp.settings import Settings


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    d = tmp_path_factory.mktemp("val")
    paths = td.build(str(d / "input"))
    s = Settings()
    s.l3_config_file, s.report_files, s.entity_export_paths = paths["config"], [paths["report"]], [paths["entities"]]
    s.steward_decisions_file, s.known_matches_file = paths["steward"], paths["known"]
    s.entity_types, s.target_rule, s.output_dir, s.resume = [], "", str(d / "out"), False
    fp_res, fp_files, probe = rp.run_reltio_fp(s)
    fn_res, fn_files = rp.run_reltio_fn(s)
    return {"s": s, "paths": paths, "fp": fp_res, "fp_files": fp_files, "probe": probe, "fn": fn_res,
            "fn_files": fn_files, "out": str(d / "out")}


def _fp(data, a, b, rule):
    return next(r for r in data["fp"].pairs if r["PairID"] == f"entities/{a}||entities/{b}" and r["MatchRuleName"] == rule)


def _fn(data):
    return {r["PairID"].replace("entities/", ""): r for r in data["fn"].pairs}


# ---------------------------------------------------------------- FP (scenarios 1-4, 8-11)
def test_fp_exact_match_multiple_rules_confirmed(data):                      # 1, 11
    for rule in ("R1_ExactNameDobNPI", "R2_FuzzyNameDob"):
        r = _fp(data, "v01", "v02", rule)
        assert r["AnalyticalRuleEvaluation"] == "MATCH" and r["EstimatedSimilarityScore"] == 100
        assert r["FPClassification"] == "Confirmed Genuine Duplicate"


def test_fp_fuzzy_match_genuine(data):                                        # 2
    r = _fp(data, "v03", "v04", "R2_FuzzyNameDob")
    assert r["FPClassification"] == "Likely Genuine Duplicate" and "FirstName" in r["VariantAttributes"]
    attr = next(a for a in data["fp"].attributes if a["PairID"] == r["PairID"] and a["Attribute"] == "FirstName")
    assert attr["ComparisonResult"] == "VARIANT_MATCH" and 80 <= attr["AttributeScore"] < 100
    assert "NPI" not in r["UnmatchedAttributes"]          # attributes outside the rule are never compared


def test_fp_spelling_variants_and_reltio_score_kept_separately(data):         # 3
    r = _fp(data, "v05", "v06", "R4_Relevance")
    assert r["ReltioMatchScore"] == 0.86 and r["EstimatedSimilarityScore"] != r["ReltioMatchScore"]
    assert r["AnalyticalRuleEvaluation"] == "MATCH"


def test_fp_same_name_different_identifiers_is_fp(data):                      # 8, 9, 10
    r = _fp(data, "v13", "v14", "R4_Relevance")
    assert r["FPClassification"] == "Potential False Positive" and "Identifiers.Value" in r["Conflicts"]
    r = _fp(data, "v17", "v18", "R4_Relevance")
    assert r["FPClassification"] == "Confirmed False Positive"            # steward NOT_A_MATCH wins


def test_fp_nested_address(data):                                             # 7
    r = _fp(data, "v09", "v10", "R3_NameAddress")
    assert r["AnalyticalRuleEvaluation"] == "MATCH"
    att = {a["Attribute"]: a for a in data["fp"].attributes if a["PairID"] == r["PairID"]}
    assert att["Address.AddressLine1"]["Nested"].startswith("sub-attribute of Address")
    assert att["Suffix"]["ComparisonResult"] == "BOTH_NULL" and att["Suffix"]["OperatorPassed"] is True  # 5


def test_fp_overall_score_formula(data):
    """Overall = sum(score * weight) / sum(weight) over scored match conditions."""
    for r in data["fp"].pairs:
        rows = [a for a in data["fp"].attributes if a["PairID"] == r["PairID"] and a["MatchRule"] == r["MatchRule"]
                and a["InOverallScore"] and a["AttributeScore"] is not None]
        if not rows:
            continue
        expect = sum(a["AttributeScore"] * a["Weight"] for a in rows) / sum(a["Weight"] for a in rows)
        assert r["EstimatedSimilarityScore"] == pytest.approx(expect, abs=0.06)


def test_fp_explanations_name_matched_and_unmatched(data):
    r = _fp(data, "v13", "v14", "R4_Relevance")
    assert "Matched exactly" in r["Explanation"] and "Did not match" in r["Explanation"] and "Verdict" in r["Explanation"]


def test_null_probe_reports_each_mode(data):
    modes = {x["BothNullExactMode"] for x in data["probe"]}
    assert {"fail", "pass", "exclude", "RECOMMENDATION"} <= modes
    r2 = {x["BothNullExactMode"]: x for x in data["probe"] if x["MatchRuleURI"].endswith("R2_FuzzyNameDob")}
    assert r2["fail"]["ReltioRowsReproduced"] < r2["pass"]["ReltioRowsReproduced"]
    assert "'pass'" in r2["RECOMMENDATION"]["Recommendation"]
    # with the documented default the pair is not reproduced and is kept for review, not called genuine
    r = _fp(data, "v09", "v10", "R2_FuzzyNameDob")
    assert r["AnalyticalRuleEvaluation"] != "MATCH" and r["FPClassification"] != "Likely Genuine Duplicate"


# ---------------------------------------------------------------- FN (scenarios 4-6, 12-14)
def test_fn_classifications(data):
    fn = _fn(data)
    assert fn["v11||v12"]["FNConfidence"] == "Confirmed False Negative"       # 6 multi-value, steward MATCH
    assert fn["v19||v20"]["FNConfidence"] == "High-confidence potential false negative"   # 12 many crosswalks
    assert fn["v19||v20"]["ApplicableMatchRule"].startswith("R1")
    assert fn["v15||v16"]["FNConfidence"] == "Requires manual review"        # 9 NPI conflict seen by other rules
    assert fn["v23||v24"]["FNConfidence"] == "Requires manual review"        # 14 OV first names differ


def test_fn_uses_ov_values_only(data):
    att = [a for a in data["fn"].attributes if a["PairID"] == "entities/v23||entities/v24" and a["Attribute"] == "FirstName"]
    assert att and att[0]["EntityA_Value"] == "Robert" and att[0]["EntityB_Value"] == "Bob"
    assert att[0]["ComparisonResult"] == "NO_MATCH"


def test_fn_excludes_merged_known_and_reported(data):
    fn = _fn(data)
    assert "v21||v22" not in fn                    # 13 already merged (shared crosswalk)
    assert "v07||v08" not in fn                    # known link file
    assert "v01||v02" not in fn and "v03||v04" not in fn       # returned by Reltio
    ex = data["fn"].stats["excluded"]
    assert ex["already merged / same source record"] == 1 and ex["already matched / linked in Reltio"] >= 7


def test_fn_multi_value_best_pair(data):
    att = [a for a in data["fn"].attributes if a["PairID"] == "entities/v11||entities/v12" and a["Attribute"] == "Phone"]
    assert not att or att[0]["MultiValue"]


def test_fn_duplicate_export_and_counts(data):
    st = data["fn"].stats
    assert st["duplicate_uris"] == 1 and st["entities_loaded"] == 34 and st["non_ov_values_ignored"] == 2
    assert st["candidate_pairs"] < 34 * 33 / 2                  # blocking, not all pairs


def test_fn_blocking_analysis(data):
    rows = data["fn"].blocking
    assert {r["BlockingStrategy"] for r in rows} >= {"reltio_tokens", "rule_keys", "rule_phonetic", "pairs"}
    assert sum(r["MatchingCandidates"] for r in rows) > 0
    keys = [(r["BlockingStrategy"], r["BlockingKey"]) for r in rows if r["BlockingStrategy"] == "rule_keys"]
    assert len(keys) == len(set(keys))                          # identical keys from several rules generated once


def test_fn_resume_skips_evaluated_pairs(data):
    s = data["s"]
    res, _ = rp.run_reltio_fn(s, resume=True, write=False)
    assert res.stats["pairs_resumed"] == res.stats["pairs_evaluated"] and len(res.pairs) == len(data["fn"].pairs)


def test_fn_parallel_matches_serial(data, tmp_path):
    s = data["s"]
    s2 = Settings(**{**s.__dict__})
    s2.workers, s2.batch_size, s2.output_dir = 2, 2, str(tmp_path)
    res, _ = rp.run_reltio_fn(s2, resume=False, write=False)
    assert sorted((r["PairID"], r["FNConfidence"]) for r in res.pairs) == \
        sorted((r["PairID"], r["FNConfidence"]) for r in data["fn"].pairs)


def test_custom_blocking_keys():
    attrs, trans = parse_custom_key("FirstName:phonetic+DoB:year", "HCP")
    assert attrs[0].endswith("/FirstName") and trans == ["phonetic", "year"]
    groups = load_groups(td.rules_config())
    names = [s.name for s in derive_strategies(groups, ["custom"], ["LastName+DoB:year"])]
    assert names == ["custom:LastName+DoB:year"]
    with pytest.raises(ValueError):
        derive_strategies(groups, ["nonsense"])


# ---------------------------------------------------------------- reports
def test_fp_workbook_sheets(data):
    wb = openpyxl.load_workbook(data["fp_files"]["fp_workbook"], read_only=True)
    assert wb.sheetnames[:3] == ["FP Summary", "FP Pair Details", "FP Attribute Details"]
    header = [c.value for c in next(wb["FP Pair Details"].iter_rows(min_row=3, max_row=3))]
    for col in ("EntityA", "EntityB", "MatchRule", "ReltioMatchScore", "EstimatedSimilarityScore",
                "AnalyticalRuleEvaluation", "FPClassification", "Explanation"):
        assert col in header


def test_fn_workbook_sheets(data):
    wb = openpyxl.load_workbook(data["fn_files"]["fn_workbook"], read_only=True)
    assert wb.sheetnames[:4] == ["FN Summary", "FN Pair Details", "FN Attribute Details", "Blocking Analysis"]


def test_client_reports(data, tmp_path):
    files = write_client_reports(str(tmp_path), data["fp"], data["fn"])
    html = open(files["client_html"], encoding="utf-8").read()
    assert "False positive analysis" in html and "False negative analysis" in html
    assert "Robert" not in html and "entities/v" not in html          # no attribute values / ids shared
    assert openpyxl.load_workbook(files["client_xlsx"]).sheetnames[0] == "Overview"


# ---------------------------------------------------------------- configuration
def test_config_shapes():
    cfg = td.rules_config()
    et = cfg["entityTypes"][0]
    for src in (cfg, et, et["matchGroups"], json.dumps(cfg), {"matchGroups": et["matchGroups"]}):
        assert len(load_groups(src)) == 5
    with pytest.raises(ConfigError):
        load_rule_config({"foo": 1})


def test_missing_config_is_a_clear_error(tmp_path):
    s = Settings()
    s.l3_config_file = str(tmp_path / "nope.json")
    with pytest.raises(rp.ConfigMissing):
        rp.resolve_groups(s)


def test_catalogues_list_comparators_and_params():
    groups = load_groups(td.rules_config())
    rows = attribute_catalog(groups)
    jw = next(r for r in rows if r["MatchRule"] == "R3_NameAddress" and r["Attribute"] == "FirstName")
    assert jw["Comparator"] == "JaroWinklerComparator"
    gender = next(r for r in rows if r["MatchRule"] == "R5_NPIOnly" and r["Attribute"] == "Gender")
    assert "notExactSame" in gender["Operators"] and gender["Comparator"].endswith("(default)")
    assert {r["RuleType"] for r in rule_catalog(groups)} == {"automatic", "suspect", "relevance_based"}


def test_compare_configs():
    a = td.rules_config()
    b = json.loads(json.dumps(a))
    gs = b["entityTypes"][0]["matchGroups"]
    gs[1]["rule"]["comparatorClasses"]["mapping"][0]["class"] = "com.reltio.match.comparator.SoundexComparator"
    del gs[4]
    diff = compare_configs(a, b)
    assert {(d["MatchRule"], d["Change"]) for d in diff} == {("R2_FuzzyNameDob", "CHANGED"), ("R5_NPIOnly", "REMOVED")}
    assert any(d["Aspect"] == "comparator[FirstName]" and "Soundex" in d["Target"] for d in diff)


# ---------------------------------------------------------------- report parser
def test_report_without_rule_column_and_alias_uris(tmp_path, data):
    p = tmp_path / "pm.csv"
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["EntityA", "EntityB", "src_FirstName", "match_FirstName"])
        w.writerow(["entities/v03", "entities/v04", "Katherine", "Catherine"])
    rows = list(read_potential_matches([str(p)]))
    assert rows[0].rule_uri == INFER_RULE and rows[0].src_uri == "entities/v03"
    res, _, _ = rp.run_reltio_fp(data["s"], report_paths=[str(p)], write=False, run_null_probe=False)
    assert res.pairs[0]["RuleSource"].startswith("inferred")


def test_report_xlsx(tmp_path, data):
    import pandas as pd
    df = pd.read_csv(data["paths"]["report"], dtype=str)
    x = tmp_path / "pm.xlsx"
    df.to_excel(x, index=False)
    assert len(list(read_potential_matches([str(x)]))) == len(df)


def test_similar_but_rule_fails_is_separate():
    """High similarity does not imply a rule pass (NOT condition)."""
    from fpnp import synthetic
    groups = load_groups(synthetic.synthetic_config())
    eng = RuleEngine(groups, EngineContext())
    attrs = list(dict.fromkeys(a for g in groups for a in g.attributes()))
    recs = {e["uri"][9:]: build_record(e, attrs) for e in synthetic.synthetic_entities()}
    hh = next(g for g in groups if g.name == "SynHousehold")
    ps = PairScorer(groups, eng).score(hh, recs["e01"], recs["e02"])
    assert ps.overall == 100 and not ps.outcome.matched
    not_row = next(a for a in ps.attributes if a.attribute == "FirstName")
    assert not_row.role.startswith("NOT") and not not_row.in_score
