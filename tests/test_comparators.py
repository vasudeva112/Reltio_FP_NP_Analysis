"""Comparators, cleansers, registry extension."""

import pytest

from fpnp import comparator_registry as registry
from fpnp.attribute_extractor import AttrValues, Leaf, PRESENT
from fpnp.cleansers import GroupCleansers
from fpnp.comparator_validation import COMPARATOR_CASES
from fpnp.comparators.base import UNSUPPORTED, CompareResult, Comparator, EngineContext
from fpnp.comparators.edit_distance import dynamic_allowed_distance
from fpnp.exceptions_log import LOG
from fpnp.rule_config_parser import parse_match_group


@pytest.mark.parametrize("cls,params,left,right,expected,note", COMPARATOR_CASES,
                         ids=[f"{c[0]}:{c[2]}~{c[3]}" for c in COMPARATOR_CASES])
def test_comparator_cases(ctx, cls, params, left, right, expected, note):
    assert registry.create(cls, params, ctx).compare(left, right).equal is expected


@pytest.mark.parametrize("length,edits", [(1, 1), (6, 1), (7, 2), (10, 2), (11, 3), (20, 3), (21, 4), (30, 4), (31, 5)])
def test_dynamic_dl_table(length, edits):
    assert dynamic_allowed_distance(length) == edits


def test_jaro_winkler_threshold_parameter(ctx):
    loose = registry.create("JaroWinklerComparator", {"similarityThreshold": "0.8"}, ctx)
    strict = registry.create("JaroWinklerComparator", {"similarityThreshold": "0.95"}, ctx)
    assert loose.compare("Dwayne", "Duane").equal and not strict.compare("Dwayne", "Duane").equal


def test_relevance_is_graded_for_fuzzy_classes(ctx):
    r = registry.create("DynamicDamerauLevenshteinDistance", {}, ctx).compare("Jonathan", "Jonathon")
    assert 0 < r.relevance < 1


def test_registry_accepts_new_class_without_engine_change(ctx):
    @registry.register
    class AlwaysEqualTestComparator(Comparator):
        RELTIO_NAME = "AlwaysEqualTestComparator"

        def compare(self, a, b):
            return CompareResult(True, 1.0)

    try:
        c = registry.create("com.example.AlwaysEqualTestComparator", {}, ctx)
        assert c.compare("a", "b").equal
    finally:
        registry.REGISTRY.pop("AlwaysEqualTestComparator")


def test_unknown_comparator_is_reported_not_silent(ctx):
    c = registry.create("com.reltio.match.comparator.NoSuchComparator", {}, ctx)
    assert c.status == UNSUPPORTED and not c.compare("a", "a").equal
    assert LOG.counts["UNSUPPORTED_COMPARATOR"] == 1


def _group(cleanse, null_values=None):
    P = "configuration/entityTypes/T/attributes/"
    return parse_match_group({
        "uri": "configuration/entityTypes/T/matchGroups/G", "type": "suspect",
        "rule": {"cleanse": cleanse, "nullValues": null_values or [], "and": {"exact": [P + "A"]}}})


def test_null_values_operator_replaces_listed_value(ctx):
    P = "configuration/entityTypes/T/attributes/"
    g = _group([], [{"attribute": P + "A", "value": "1900-01-01"}])
    gc = GroupCleansers(g, ctx)
    pv = gc.prepare(P + "A", AttrValues(PRESENT, (Leaf("1900-01-01", "x"), Leaf("1980-02-02", "y"))))
    assert pv.values == ["1980-02-02"] and pv.nulled == ("1900-01-01",)
    pv2 = gc.prepare(P + "A", AttrValues(PRESENT, (Leaf("1900-01-01", "x"),)))
    assert not pv2.has_value and pv2.state == "NULL_VALUE"


def test_name_dictionary_cleanser_identity_without_dictionary_and_mapping_with_one():
    P = "configuration/entityTypes/T/attributes/"
    cl = [{"cleanseAdapter": "com.reltio.cleanse.impl.NameDictionaryCleanser",
           "mappings": [{"attribute": P + "A", "cleanseAttribute": P + "A"}]}]
    g = _group(cl)
    plain = GroupCleansers(g, EngineContext()).prepare(P + "A", AttrValues(PRESENT, (Leaf("Liz", ""),)))
    assert plain.values == ["Liz"] and "NameDictionaryCleanser:identity" in plain.cleanse_notes
    mapped = GroupCleansers(g, EngineContext(name_dictionary={"liz": "Elizabeth"})).prepare(
        P + "A", AttrValues(PRESENT, (Leaf("Liz", ""),)))
    assert mapped.values == ["Elizabeth"]
    assert LOG.counts["APPROXIMATION"] >= 1


def test_regexp_cleanser_with_local_rules():
    import re
    P = "configuration/entityTypes/T/attributes/"
    cl = [{"cleanseAdapter": "com.reltio.cleanse.impl.RegexpReplaceCleanser",
           "mappings": [{"attribute": P + "A", "cleanseAttribute": P + "A"}]}]
    ctx = EngineContext(regexp_rules=[(re.compile(r"^junior$", re.I), "JR")])
    pv = GroupCleansers(_group(cl), ctx).prepare(P + "A", AttrValues(PRESENT, (Leaf("Junior", ""),)))
    assert pv.values == ["JR"]
