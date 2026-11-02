"""End-to-end on the synthetic dataset with explicitly designed outcomes.
These results say nothing about production FP/FN rates."""

import csv
import os

import openpyxl
import pytest

from fpnp import pipeline, synthetic
from fpnp.pair_deduplication import pair_key
from fpnp.settings import Settings

G = synthetic.G


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    base = tmp_path_factory.mktemp("syn")
    paths = synthetic.build(str(base / "input"))
    s = Settings()
    s.l3_config_file, s.report_files = paths["config"], [paths["report"]]
    s.entity_export_paths, s.steward_decisions_file = [paths["entities"]], paths["steward"]
    s.entity_types, s.target_rule, s.output_dir, s.resume = ["Person"], "", str(base / "out"), False
    s.excel_row_cap = 5
    files = pipeline.run(s, "all", recall=True)

    def rows(name):
        with open(files[name], encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    return files, rows("fp_rule_level"), rows("fp_pair_level"), rows("fn"), s


def fp(rows, a, b, group):
    return next(r for r in rows if r["PairKey"] == pair_key(a, b) and r["MatchGroupURI"] == G + group)


def fn(rows, a, b, group):
    return [r for r in rows if r["PairKey"] == pair_key(a, b) and r["MatchGroupURI"] == G + group]


def test_fp_classifications(run):
    _, rows, pairs, _, _ = run
    assert fp(rows, "e01", "e02", "SynExactNameDob")["Classification"] == "Confirmed True Positive"
    r = fp(rows, "e03", "e04", "SynRelevance")
    assert r["Classification"] == "Confirmed False Positive"
    assert "LOW_MARGIN" in r["RiskIndicators"] and "PERMISSIVE_COMPARATOR" in r["RiskIndicators"]
    assert r["FailedAttributes"] == "DoB"
    d = fp(rows, "e05", "e20", "SynRelevance")
    assert d["PythonMatched"] == "False" and "PYTHON_DISAGREES" in d["RiskIndicators"]
    assert d["Classification"] in ("Suspected False Positive", "Requires Steward Review")
    assert "CROSS_INSTANCE_NESTED_MATCH" in fp(rows, "e18", "e19", "SynIdFuzzyName")["RiskIndicators"]
    # one pair-level row per unique pair, rule-level rows preserved
    p = next(x for x in pairs if x["PairKey"] == pair_key("e01", "e02"))
    assert p["RuleLevelRows"] == "3" and p["Classification"] == "Confirmed True Positive"
    assert len(pairs) == len({r["PairKey"] for r in rows})


def test_fn_classifications(run):
    _, _, _, rows, _ = run
    (c,) = fn(rows, "e07", "e08", "SynExactNameDob")
    assert c["Classification"] == "Confirmed False Negative" and c["ReasonCode"] == "NULL_HANDLING_MISMATCH"
    (t,) = fn(rows, "e11", "e12", "SynIdFuzzyName")
    assert t["Classification"] == "Suspected False Negative"
    assert t["ReasonCode"] == "TOKENIZATION_MISMATCH" and t["CandidateSource"] == "BLOCKING_ONLY"
    assert fn(rows, "e13", "e14", "SynExactNameDob") == []               # negative rule
    assert fn(rows, "e15", "e17", "SynHousehold") == []                  # exactOrAllNull one-sided null
    assert fn(rows, "e15", "e16", "SynHousehold")[0]["Classification"] == "Reltio Match"
    assert all(r["Classification"] == "Reltio Match" for r in fn(rows, "e01", "e02", "SynIdFuzzyName"))
    assert not any(r["EntityURI1"] == r["EntityURI2"] for r in rows)


def test_workbook_structure_and_caps(run):
    files, *_ = run
    wb = openpyxl.load_workbook(files["workbook"], read_only=True)
    assert wb.sheetnames[:7] == ["Executive Summary", "Potential Match Analysis", "False Positive Candidates",
                                 "False Negative Candidates", "Rule-Level Summary", "Comparator Validation",
                                 "Processing Exceptions"]
    title = wb["Comparator Validation"]["A1"].value
    assert title.startswith("Comparator Validation: CAPPED EXTRACT - showing 5 of")
    assert os.path.isfile(files["validation"]) and os.path.isfile(files["metadata"])


def test_executive_summary_labels_bases(run):
    files, *_ = run
    wb = openpyxl.load_workbook(files["workbook"], read_only=True)
    rows = list(wb["Executive Summary"].iter_rows(min_row=3, values_only=True))
    prec = next(r for r in rows if r[1] == "Precision on labelled pairs")
    assert "confirmed labels" in prec[3]
    sus = next(r for r in rows if r[1] == "Suspected false positives (pairs)")
    assert sus[3].startswith("estimate")


def test_resume_does_not_reevaluate(run, tmp_path):
    files, _, _, rows, s = run
    s.resume = True
    files2 = pipeline.run(s, "fn")
    with open(files2["fn"], encoding="utf-8-sig") as fh:
        again = list(csv.DictReader(fh))
    assert len(again) == len(rows)
    import json
    with open(files2["metadata"], encoding="utf-8") as fh:
        meta = json.load(fh)
    assert meta["fn_metrics"]["pairs_already_evaluated_resume"] == meta["fn_metrics"]["pairs_evaluated"]


def test_html_report_written_and_rebuildable(run):
    from fpnp import html_report
    files, *_ , s = run
    page = open(files["html_report"], encoding="utf-8").read()
    assert "Key findings" in page and "Missed matches" in page and "Not run in this analysis" not in page
    assert "Confirmed false positive" in page and "<script>" in page and "http" not in page.split("<body>")[1][:200]
    data = html_report.load_outputs(s.output_dir)
    again = html_report.write(os.path.join(s.output_dir, "rebuilt.html"), settings=s,
                              limitations=pipeline.LIMITATIONS, **data)
    assert "Key findings" in open(again, encoding="utf-8").read()
