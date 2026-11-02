"""Comparator score view: Double Metaphone code length, what-if scoring, result classes, segment flag."""

import itertools
import os

import openpyxl
import pytest

from fpnp import synthetic
from fpnp.comparators.base import EngineContext
from fpnp.comparators.phonetic import DoubleMetaphoneComparator, double_metaphone
from fpnp.fp_scores import (AGREE, DISAGREE, NULL, PARTIAL, PHONETIC, VARIANT, ComparatorScorer, classify_leaf,
                            scored_nodes)
from fpnp.operators import ONE_SIDED_NULL, LeafResult
from fpnp.report_reader import ReportRow
from fpnp.settings import Settings


def test_double_metaphone_codes_are_four_characters_by_default():
    ctx = EngineContext()
    assert all(len(c) <= 4 for c in double_metaphone("tsiourouti jr"))
    assert DoubleMetaphoneComparator(ctx=ctx).compare("Tsiourouti", "Tsiourouti Jr").equal
    try:
        unlimited = EngineContext(double_metaphone_max_length=0)
        assert not DoubleMetaphoneComparator(ctx=unlimited).compare("Tsiourouti", "Tsiourouti Jr").equal
    finally:
        EngineContext()                       # restore the default for other tests


def test_what_if_reproduces_evaluate(syn_groups, syn_engine, syn_records):
    checked = 0
    for g in syn_groups.values():
        if not g.is_relevance:
            continue
        for a, b in itertools.combinations(sorted(syn_records), 2):
            ev = syn_engine.evaluate(g, syn_records[a], syn_records[b])
            if ev.ignored_constraints or not ev.applicable:
                continue
            rel = {lf.attribute: lf.relevance for lf in ev.attribute_leaves()}
            passed, r = syn_engine.what_if(g, rel)
            assert r == pytest.approx(ev.relevance, abs=1e-6)
            assert passed == (syn_engine._threshold(g.thresholds, ev.relevance) is not None)
            checked += 1
    assert checked > 10


def test_what_if_single_failure_uses_weights(syn_groups, syn_engine):
    g = syn_groups["SynRelevance"]
    names = {n.attribute.rsplit("/", 1)[-1]: n.attribute for n in scored_nodes(g)}
    total = sum(g.weight_for(a) for a in names.values())
    ok, r = syn_engine.what_if(g, failing=[names["FirstName"]])
    assert r == pytest.approx(1 - g.weight_for(names["FirstName"]) / total)
    assert ok == (r >= min(t.lo for t in g.thresholds))
    ok2, _ = syn_engine.what_if(g, failing=[names["FirstName"], names["LastName"]])
    assert not ok2


def _leaf(**kw):
    base = dict(op="fuzzy", attribute="a", passed=True, relevance=1.0, left="x", right="x",
                comparator="DoubleMetaphoneComparator", matched_values=("x", "x"))
    base.update(kw)
    return LeafResult(**base)


@pytest.mark.parametrize("kw,expected", [
    (dict(), AGREE),
    (dict(left="Jon", right="John", matched_values=("Jon", "John")), VARIANT),
    (dict(left="Kathy", right="Cottey", matched_values=("Kathy", "Cottey")), PHONETIC),
    (dict(passed=False, relevance=0.0, left="Mary", right="Zed", matched_values=("", "")), DISAGREE),
    (dict(passed=False, relevance=0.9, left="12 Main St", right="12 Main Street", matched_values=("", "")),
     PARTIAL),
    (dict(null_case=ONE_SIDED_NULL, left="<MISSING>", right="x"), NULL),
])
def test_classify_leaf(kw, expected):
    result, _ = classify_leaf(_leaf(**kw), str.lower, 0.85, relevance_mode=True)
    assert result == expected


def test_segment_requires_both_entities(syn_groups, syn_engine):
    s = Settings()
    s.segment_label, s.segment_crosswalk_regex = "Bene Record", r"^CIF:9000000"
    sc = ComparatorScorer(list(syn_groups.values()), syn_engine, s)
    row = lambda a, b: ReportRow("", "", "e1", "e2", a, b, {}, {}, "", 0)
    assert sc.segment(row("CIF:900000012", "CIF:12 | CIF:9000000555"))
    assert not sc.segment(row("CIF:900000012", "CIF:123"))
    s.segment_crosswalk_regex = ""
    assert ComparatorScorer(list(syn_groups.values()), syn_engine, s).segment(row("x", "y")) is None


def test_score_report_written(tmp_path):
    from fpnp import pipeline
    paths = synthetic.build(str(tmp_path / "input"))
    s = Settings()
    s.l3_config_file, s.report_files = paths["config"], [paths["report"]]
    s.entity_types, s.target_rule, s.output_dir, s.resume = ["Person"], "", str(tmp_path / "out"), False
    files = pipeline.run(s, "fp")
    wb = openpyxl.load_workbook(files["score_workbook"], read_only=True)
    assert {"Summary", "False_Positives", "Attribute_Scores", "Relevance_Weights", "Rule_Tolerance",
            "Result_Definitions"} <= set(wb.sheetnames)
    detail = list(wb["False_Positives"].iter_rows(min_row=2, values_only=True))
    header, rows = detail[0], detail[1:]
    rec = dict(zip(header, next(r for r in rows if "e20" in str(r[0]))))
    assert rec["FirstName Result"] == DISAGREE and rec["Band"] == "(below every threshold)"
    assert "FirstName -0.300" in rec["Score Impact"]
    page = open(files["html_report"], encoding="utf-8").read()
    assert "Comparator score analysis" in page and "Relevance weights by rule" in page
    assert os.path.isfile(os.path.join(s.output_dir, "detail", "rule_tolerance.csv"))
