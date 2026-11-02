"""Operators: null / missing / empty semantics, constraints, case and whitespace."""

import pytest

from fpnp import comparator_registry as registry
from fpnp.attribute_extractor import (EMPTY_LIST, EMPTY_STRING, MISSING, NO_OV_VALUE, NULL, PRESENT, WHITESPACE,
                                      extract)
from fpnp.comparator_validation import (CONSTRAINT_CASES, EXPECTED_OPS, MISSING as MISSING_MARK, NULL_CASES,
                                        prepared, run_catalogue)
from fpnp.comparators.base import EngineContext
from fpnp.operators import BOTH_NULL, ONE_SIDED_NULL, evaluate_attribute_op, evaluate_constraint

CASES = [(op, case, l, r, exp) for op, exps in EXPECTED_OPS.items() for (case, l, r), exp in zip(NULL_CASES, exps)]


@pytest.mark.parametrize("op,case,left,right,expected", CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
def test_operator_null_matrix(ctx, op, case, left, right, expected):
    cmp = registry.create("BasicStringComparator", {}, ctx)
    res = evaluate_attribute_op(op, "A", cmp, ctx, prepared(left, ctx), prepared(right, ctx))
    assert res.passed is expected


def test_one_sided_null_separates_exactOrNull_from_exactOrAllNull(ctx):
    cmp = registry.create("BasicStringComparator", {}, ctx)
    for l, r in ((None, "abc"), ("abc", None), (MISSING_MARK, "abc"), ("", "abc")):
        eon = evaluate_attribute_op("exactOrNull", "A", cmp, ctx, prepared(l, ctx), prepared(r, ctx))
        eoan = evaluate_attribute_op("exactOrAllNull", "A", cmp, ctx, prepared(l, ctx), prepared(r, ctx))
        assert eon.passed is True and eoan.passed is False
        assert eon.null_case == ONE_SIDED_NULL
    both_eon = evaluate_attribute_op("exactOrNull", "A", cmp, ctx, prepared(None, ctx), prepared(None, ctx))
    both_eoan = evaluate_attribute_op("exactOrAllNull", "A", cmp, ctx, prepared(None, ctx), prepared(None, ctx))
    assert both_eon.passed and both_eoan.passed          # matches the user's own tenant observation
    assert both_eon.null_case == BOTH_NULL


@pytest.mark.parametrize("op,values,left,right,expected", CONSTRAINT_CASES)
def test_constraints(ctx, op, values, left, right, expected):
    assert evaluate_constraint(op, "A", values, ctx, prepared(left, ctx), prepared(right, ctx)).passed is expected


def test_value_states_are_distinct():
    assert extract({}, ["A"]).state == MISSING
    assert extract({"A": None}, ["A"]).state == NULL
    assert extract({"A": []}, ["A"]).state == EMPTY_LIST
    assert extract({"A": [{"value": "", "ov": True}]}, ["A"]).state == EMPTY_STRING
    assert extract({"A": [{"value": "  ", "ov": True}]}, ["A"]).state == WHITESPACE
    assert extract({"A": [{"value": "x", "ov": False}]}, ["A"]).state == NO_OV_VALUE
    assert extract({"A": [{"value": "x", "ov": False}]}, ["A"], ov_only=False).state == PRESENT


def test_blank_kept_as_value_when_configured():
    av = extract({"A": [{"value": "  ", "ov": True}]}, ["A"], treat_blank_as_null=False)
    assert av.state == PRESENT and av.values == ["  "]


def test_nested_extraction_keeps_instances_and_skips_non_ov():
    attrs = {"Identifiers": [
        {"ov": True, "uri": "i0", "value": {"Type": [{"value": "SSN", "ov": True}], "Value": [{"value": "1", "ov": True}]}},
        {"ov": False, "uri": "i1", "value": {"Type": [{"value": "EIN", "ov": True}], "Value": [{"value": "2", "ov": True}]}},
        {"ov": True, "uri": "i2", "value": {"Type": [{"value": None, "ov": True}]}},
    ]}
    t = extract(attrs, ["Identifiers", "Type"])
    v = extract(attrs, ["Identifiers", "Value"])
    assert t.values == ["SSN"] and [l.instance for l in t.leaves] == ["i0"]
    assert v.values == ["1"]
    assert NULL in t.empty_states


def test_case_sensitivity_switch():
    ci, cs = EngineContext(case_insensitive=True), EngineContext(case_insensitive=False)
    assert registry.create("BasicStringComparator", {}, ci).compare("ABC", "abc").equal
    assert not registry.create("BasicStringComparator", {}, cs).compare("ABC", "abc").equal
    # equals/in are documented as case-insensitive regardless of the switch
    assert evaluate_constraint("equals", "A", ["Y"], cs, prepared("y", cs), prepared("Y", cs)).passed


def test_catalogue_all_pass():
    bad = [r for r in run_catalogue() if r["Outcome"] != "PASS"]
    assert not bad, bad
