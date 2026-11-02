"""Reltio comparator engine: every comparator class, operator null semantics, normalisation, attribute scores."""

import pytest

from fpnp import comparator_registry as registry
from fpnp.attribute_extractor import build_record
from fpnp.comparator_validation import prepared
from fpnp.comparators.base import EngineContext
from fpnp.operators import evaluate_attribute_op
from fpnp.reltio_engine.comparator_engine import comparator_kind
from fpnp.reltio_engine.normalization_engine import (analytical_similarity, comparator_view, parse_date,
                                                     value_kind)
from fpnp.reltio_engine.scoring_config import ScoringConfig
from fpnp.reltio_engine.scoring_engine import PairScorer
from fpnp.rule_config_parser import parse_match_groups
from fpnp.rule_evaluator import RuleEngine

# ---------------------------------------------------------------- every comparator class (equal / not equal)
CASES = [
    ("BasicStringComparator", {}, " John ", "john", True),
    ("BasicStringComparator", {}, "John", "Jon", False),
    ("DoubleMetaphoneComparator", {}, "Katherine", "Catherine", True),
    ("DoubleMetaphoneComparator", {}, "Smith", "Jones", False),
    ("SoundexComparator", {}, "Robert", "Rupert", True),
    ("SoundexComparator", {}, "Robert", "Smith", False),
    ("MetaphoneComparator", {}, "Smith", "Smyth", True),
    ("MetaphoneComparator", {}, "Smith", "Brown", False),
    ("JaroWinklerComparator", {"similarityThreshold": "0.9"}, "Jonathan", "Jonathon", True),
    ("JaroWinklerComparator", {"similarityThreshold": "0.9"}, "Jonathan", "Peter", False),
    ("DynamicDamerauLevenshteinDistance", {}, "Jonathan", "Jonathon", True),
    ("DynamicDamerauLevenshteinDistance", {}, "Ann", "Bob", False),
    ("DamerauLevenshteinDistance", {}, "Smith", "Smiht", True),
    ("DamerauLevenshteinDistance", {}, "Smith", "Brown", False),
    ("PhoneNumberComparator", {}, "+1 (214) 555-0101", "214-555-0101", True),
    ("PhoneNumberComparator", {}, "123456789", "123456789", False),        # < 10 digits never equal
    ("AddressLineComparator", {}, "10 Main St", "10 Main Street", True),
    ("AddressLineComparator", {}, "10 Main St", "99 Oak Avenue", False),
    ("RangeNumericComparator", {"threshold": "1"}, "1980", "1981", True),
    ("RangeNumericComparator", {"threshold": "1"}, "1980", "1983", False),
    ("CustomComparator", {"groups": [{"className": "RangeNumericComparator", "classParams": {"threshold": "1"},
                                      "pattern": r"^(\d{4})"}]}, "1980-01-01", "1981-05-05", True),
    ("CustomComparator", {"groups": [{"className": "RangeNumericComparator", "classParams": {"threshold": "1"},
                                      "pattern": r"^(\d{4})"}]}, "1980-01-01", "1990-01-01", False),
    ("CrossMultiComparator", {}, "A", "a", True),
]


@pytest.mark.parametrize("cls,params,a,b,expected", CASES, ids=[f"{c[0]}:{c[2]}~{c[3]}" for c in CASES])
def test_every_comparator_class(ctx, cls, params, a, b, expected):
    assert registry.create(cls, params, ctx).compare(a, b).equal is expected


def test_every_registered_comparator_has_a_case():
    covered = {c[0] for c in CASES}
    assert set(registry.registered()) - covered == set()


def test_unknown_comparator_is_unsupported_never_equal(ctx):
    c = registry.create("com.reltio.match.comparator.SomethingNew", {}, ctx)
    assert c.status == "Unsupported" and c.compare("x", "x").equal is False


def test_case_sensitivity_is_configurable():
    strict = registry.create("BasicStringComparator", {}, EngineContext(case_insensitive=False))
    assert not strict.compare("John", "john").equal and strict.compare(" John ", "John").equal


def test_cross_multi_permutations(ctx):
    cmp = registry.create("CrossMultiComparator", {}, ctx)
    basic = registry.create("BasicStringComparator", {}, ctx)
    r = cmp.compare_groups([["John"], ["Smith"]], [["Smith"], ["John"]], [basic, basic])
    assert r.equal and r.relevance == 1.0


# ---------------------------------------------------------------- operator null semantics
B = registry.create("BasicStringComparator")
NULL_MATRIX = [
    # op, left, right, expected_passed
    ("exact", "a", "a", True), ("exact", None, "a", False), ("exact", None, None, False),
    ("fuzzy", None, None, False),
    ("exactOrNull", "a", "b", False), ("exactOrNull", None, "a", True), ("exactOrNull", None, None, True),
    ("exactOrAllNull", None, "a", False), ("exactOrAllNull", None, None, True), ("exactOrAllNull", "a", "a", True),
    ("exactOrNullOrAllNull", None, "a", True), ("exactOrNullOrAllNull", None, None, True),
    ("exactOrNullOrAllNull", "a", "b", False),
    ("notExact", "a", "b", True), ("notExact", "a", "a", False), ("notExact", None, "a", True),
    ("exactOrAllNull", [None, ""], [], True),            # all-null collection on both sides
    ("exactOrAllNull", [None, "a"], [], False),          # one populated value -> not all-null
]


@pytest.mark.parametrize("op,left,right,expected", NULL_MATRIX, ids=[f"{c[0]}:{c[1]}~{c[2]}" for c in NULL_MATRIX])
def test_operator_null_matrix(ctx, op, left, right, expected):
    lr = evaluate_attribute_op(op, "A", B, ctx, prepared(left, ctx), prepared(right, ctx))
    assert lr.passed is expected


@pytest.mark.parametrize("mode,passed,excluded", [("fail", False, False), ("pass", True, False),
                                                  ("exclude", True, True)])
def test_both_null_exact_mode(mode, passed, excluded):
    ctx = EngineContext(both_null_exact_mode=mode)
    lr = evaluate_attribute_op("exact", "A", B, ctx, prepared(None, ctx), prepared(None, ctx))
    assert lr.passed is passed and lr.excluded is excluded
    one = evaluate_attribute_op("exact", "A", B, ctx, prepared(None, ctx), prepared("x", ctx))
    assert one.passed is False                      # one-sided null never passes exact


def test_both_null_mode_validated():
    with pytest.raises(ValueError):
        EngineContext(both_null_exact_mode="maybe")


def _rel_group(op="exact"):
    P = "configuration/entityTypes/T/attributes/"
    return parse_match_groups({"entityTypes": [{"uri": "configuration/entityTypes/T", "matchGroups": [{
        "uri": "configuration/entityTypes/T/matchGroups/R", "type": "relevance_based",
        "rule": {"and": {op: [P + "A", P + "B"]},
                 "actionThresholds": [{"type": "potential_match", "threshold": "0.9-1.0"}]}}]}]})[0]


def _rec(uri, **vals):
    P = "configuration/entityTypes/T/attributes/"
    ent = {"uri": uri, "type": "configuration/entityTypes/T",
           "attributes": {k: [{"value": v, "ov": True}] for k, v in vals.items()}}
    return build_record(ent, [P + "A", P + "B"])


@pytest.mark.parametrize("mode,matched,relevance", [("fail", False, 0.5), ("pass", True, 1.0), ("exclude", True, 1.0)])
def test_both_null_mode_in_relevance_rule(mode, matched, relevance):
    g = _rel_group()
    eng = RuleEngine([g], EngineContext(both_null_exact_mode=mode))
    ev = eng.evaluate(g, _rec("entities/1", A="x"), _rec("entities/2", A="x"))
    assert ev.matched is matched and ev.relevance == pytest.approx(relevance)


# ---------------------------------------------------------------- normalisation / analytical similarity
def test_comparator_views(ctx):
    assert comparator_view(registry.create("PhoneNumberComparator"), "+1 (214) 555-0101", ctx) == "2145550101"
    assert "DM" in comparator_view(registry.create("DoubleMetaphoneComparator"), "Smith", ctx)
    assert comparator_view(registry.create("AddressLineComparator"), "10 Main Street", ctx) == "10 main"
    assert comparator_view(registry.create("BasicStringComparator"), "  JOHN   Q ", ctx) == "john q"


def test_typed_similarity(ctx):
    assert parse_date("1975-03-04") == (1975, 3, 4) and parse_date("03/04/1975") == (1975, 3, 4)
    assert value_kind("1975-03-03", "1975-03-04") == "date"
    assert analytical_similarity("1975-03-03", "1975-03-04", ctx)[0] == pytest.approx(0.9)
    assert analytical_similarity("1975-03-04", "1975-04-03", ctx)[0] == pytest.approx(0.85)   # day/month swap
    s, kind = analytical_similarity("1234567890", "9876543210", ctx)
    assert kind == "digits" and s < 0.3                     # different identifiers are not "similar"
    assert analytical_similarity("John", "Jon", ctx)[0] > 0.9


def test_comparator_kinds():
    assert comparator_kind("BasicStringComparator") == "exact"
    assert comparator_kind("DoubleMetaphoneComparator") == "phonetic"
    assert comparator_kind("JaroWinklerComparator") == "fuzzy"


# ---------------------------------------------------------------- attribute scores per comparator kind
def _score_rows(groups, a, b, cfg=None, ctx=None):
    eng = RuleEngine(groups, ctx or EngineContext())
    return {r["Attribute"]: r for r in PairScorer(groups, eng, cfg).score(groups[0], a, b).attribute_rows()}


def test_exact_mismatch_scores_zero_with_near_miss_note():
    g = _rel_group()
    rows = _score_rows([g], _rec("entities/1", A="1975-03-03", B="x"), _rec("entities/2", A="1975-03-04", B="x"))
    assert rows["A"]["AttributeScore"] == 0 and rows["A"]["ComparisonResult"] == "NO_MATCH"
    assert rows["A"]["AnalyticalSimilarity"] == pytest.approx(0.9) and "Near miss" in rows["A"]["Explanation"]
    graded = _score_rows([g], _rec("entities/1", A="1975-03-03", B="x"), _rec("entities/2", A="1975-03-04", B="x"),
                         ScoringConfig.from_dict({"exact_class_mismatch": "graded"}))
    assert 40 <= graded["A"]["AttributeScore"] < 80 and graded["A"]["ComparisonResult"] == "PARTIAL"


def test_scoring_config_validation():
    with pytest.raises(ValueError):
        ScoringConfig.from_dict({"null_policy": "sometimes"})
    with pytest.raises(ValueError):
        ScoringConfig.from_dict({"bands": {"strong_min": 30, "partial_min": 60}})
    c = ScoringConfig.from_dict({"fp": {"genuine_min_score": 90}})
    assert c.fp.genuine_min_score == 90 and c.fp.candidate_max_score == 60
