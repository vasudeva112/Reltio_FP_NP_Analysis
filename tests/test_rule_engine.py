"""Rule parsing and evaluation: precedence, relevance arithmetic, negative rules, multiple groups."""

import json
import os

import pytest

from conftest import P, REAL_L3
from fpnp.attribute_extractor import build_record
from fpnp.comparators.base import EngineContext
from fpnp.exceptions_log import LOG
from fpnp.rule_config_parser import inventory, load_config, parse_match_group, parse_match_groups
from fpnp.rule_evaluator import RuleEngine

T = "configuration/entityTypes/T/attributes/"


def ent(uri, **attrs):
    a = {}
    for k, v in attrs.items():
        if isinstance(v, dict):          # nested
            a[k] = [{"ov": True, "uri": f"{uri}/{k}/{i}", "value": {s: [{"value": x, "ov": True}] for s, x in d.items()}}
                    for i, d in enumerate(v["items"])]
        elif v is None:
            a[k] = None
        else:
            a[k] = [{"value": x, "ov": True} for x in (v if isinstance(v, list) else [v])]
    return {"uri": uri, "type": "configuration/entityTypes/T", "attributes": a}


def evaluate(group_json, e1, e2, **ctx):
    g = parse_match_group(group_json)
    eng = RuleEngine([g], EngineContext(**ctx))
    attrs = g.attributes()
    return eng.evaluate(g, build_record(e1, attrs), build_record(e2, attrs))


def grp(rule, type_="suspect", **extra):
    return {"uri": "configuration/entityTypes/T/matchGroups/G", "type": type_, "rule": rule, **extra}


# ---------------------------------------------------------------- boolean logic
def test_and_or_precedence():
    # A AND (B OR C)
    rule = {"and": {"exact": [T + "A"], "or": {"exact": [T + "B", T + "C"]}}}
    base = dict(A="1", B="x", C="y")
    assert evaluate(grp(rule), ent("e1", **base), ent("e2", A="1", B="x", C="zz")).matched
    assert evaluate(grp(rule), ent("e1", **base), ent("e2", A="1", B="zz", C="y")).matched
    assert not evaluate(grp(rule), ent("e1", **base), ent("e2", A="1", B="zz", C="zz")).matched
    assert not evaluate(grp(rule), ent("e1", **base), ent("e2", A="2", B="x", C="y")).matched


def test_and_list_form_and_not():
    rule = {"and": [{"and": {"exact": [T + "A"]}}, {"not": {"in": [{"value": "PO BOX 1", "uri": T + "B"}]}}]}
    assert evaluate(grp(rule), ent("e1", A="1", B="Main"), ent("e2", A="1", B="Main")).matched
    assert not evaluate(grp(rule), ent("e1", A="1", B="PO BOX 1"), ent("e2", A="1", B="PO BOX 1")).matched


def test_top_level_operators_are_implicit_and():
    rule = {"in": [{"values": ["SSN"], "uri": T + "Ty"}], "and": {"exact": [T + "A"]}}
    assert evaluate(grp(rule), ent("e1", A="1", Ty="SSN"), ent("e2", A="1", Ty="ssn")).matched
    assert not evaluate(grp(rule), ent("e1", A="1", Ty="SSN"), ent("e2", A="1", Ty="DL")).matched


def test_negative_rule_vetoes_match():
    g = grp({"and": {"exact": [T + "A"]}},
            negativeRule={"and": {"equals": [{"uri": T + "S", "values": ["DECEASED"]}]}})
    ev = evaluate(g, ent("e1", A="1", S="DECEASED"), ent("e2", A="1", S="deceased"))
    assert ev.logic_passed and not ev.matched and ev.negative_rule_hit
    assert evaluate(g, ent("e1", A="1", S="DECEASED"), ent("e2", A="1", S="ALIVE")).matched


def test_trace_records_every_condition():
    rule = {"and": {"exact": [T + "A", T + "B"], "exactOrNull": [T + "C"]}}
    ev = evaluate(grp(rule), ent("e1", A="1", B="2"), ent("e2", A="1", B="3", C="x"))
    labels = [t.label for t in ev.trace]
    assert labels == ["AND", "AND", "exact(A)", "exact(B)", "exactOrNull(C)"]
    assert ev.failed() == ["B"] and ev.null_contributions() == ["C:exactOrNull:ONE_SIDED_NULL"]
    assert json.loads(ev.trace_json())[2]["attribute"] == "A"


# ---------------------------------------------------------------- relevance
REL_RULE = {
    "and": {"fuzzy": [T + "FN", T + "LN"], "exact": [T + "DOB"], "exactOrNull": [T + "SX"]},
    "comparatorClasses": {"mapping": [{"attribute": T + "FN", "class": "DoubleMetaphoneComparator"},
                                      {"attribute": T + "LN", "class": "DoubleMetaphoneComparator"}]},
    "weights": [{"attribute": T + "FN", "weight": 0.3}, {"attribute": T + "LN", "weight": 0.3},
                {"attribute": T + "DOB", "weight": 0.3}, {"attribute": T + "SX", "weight": 0.1}],
    "actionThresholds": [{"type": "potential_match", "threshold": "0.8-0.9", "label": "Strong"},
                         {"type": "potential_match", "threshold": "0.9-1", "label": "Super"}],
}


def test_relevance_weighted_average_and_threshold_selection():
    ev = evaluate(grp(REL_RULE, "relevance_based"), ent("e1", FN="Jon", LN="Smith", DOB="1980"),
                  ent("e2", FN="John", LN="Smith", DOB="1981"))
    assert ev.relevance == pytest.approx((0.3 + 0.3 + 0 + 0.1) / 1.0)        # 0.7
    assert not ev.matched and ev.action_type == ""
    ev2 = evaluate(grp(REL_RULE, "relevance_based"), ent("e1", FN="Jon", LN="Smith", DOB="1980", SX="Jr"),
                   ent("e2", FN="John", LN="Smith", DOB="1980", SX="Sr"))
    assert ev2.relevance == pytest.approx(0.9)
    assert ev2.matched and ev2.action_label == "Super"     # boundary 0.9 belongs to both; higher band wins


def test_relevance_default_weight_is_one_and_not_inverts():
    rule = {"and": {"exact": [T + "A"], "not": {"exact": [T + "B"]}},
            "actionThresholds": [{"type": "potential_match", "threshold": "0.5-1", "label": "x"}]}
    ev = evaluate(grp(rule, "relevance_based"), ent("e1", A="1", B="2"), ent("e2", A="1", B="2"))
    assert ev.relevance == pytest.approx(0.5)               # (1*1 + 1*(1-1)) / 2


def test_relevance_or_takes_max_weighted():
    rule = {"and": {"or": {"exact": [T + "A", T + "B"]}},
            "weights": [{"attribute": T + "A", "weight": 0.4}, {"attribute": T + "B", "weight": 0.9}],
            "actionThresholds": [{"type": "potential_match", "threshold": "0.5-1", "label": "x"}]}
    ev = evaluate(grp(rule, "relevance_based"), ent("e1", A="1", B="2"), ent("e2", A="1", B="9"))
    assert ev.relevance == pytest.approx(0.4)


NESTED = {"in": [{"values": ["SSN"], "uri": T + "Id/attributes/Type"}],
          "and": {"exact": [T + "Id/attributes/Type", T + "Id/attributes/Value", T + "N"]},
          "weights": [{"attribute": T + "Id/attributes/Type", "weight": 0.8},
                      {"attribute": T + "Id/attributes/Value", "weight": 0.8}],
          "actionThresholds": [{"type": "potential_match", "threshold": "0.75-1", "label": "x"}]}


@pytest.mark.parametrize("mode,expected", [("flat", (0.8 + 0 + 1) / 2.6), ("mean", (0.8 * 0.5 + 1) / 1.8),
                                           ("max", (0.8 + 1) / 1.8)])
def test_nested_aggregation_modes(mode, expected):
    e1 = ent("e1", N="x", Id={"items": [{"Type": "SSN", "Value": "1"}]})
    e2 = ent("e2", N="x", Id={"items": [{"Type": "SSN", "Value": "2"}]})
    ev = evaluate(grp(NESTED, "relevance_based"), e1, e2, relevance_nested_aggregation=mode)
    assert ev.relevance == pytest.approx(expected, abs=1e-6)


def test_constraint_modes_in_relevance_groups():
    e1 = ent("e1", N="x", Id={"items": [{"Type": "FRPP", "Value": "1"}]})
    e2 = ent("e2", N="x", Id={"items": [{"Type": "FRPP", "Value": "1"}]})
    veto = evaluate(grp(NESTED, "relevance_based"), e1, e2, relevance_constraint_mode="veto")
    ign = evaluate(grp(NESTED, "relevance_based"), e1, e2, relevance_constraint_mode="ignore")
    assert not veto.applicable and not veto.matched
    assert ign.applicable and ign.matched and ign.ignored_constraints
    assert ign.decisive_status() == "Approximation"


def test_cross_instance_nested_match_flagged():
    rule = {"and": {"exact": [T + "Id/attributes/Type", T + "Id/attributes/Value"]}}
    e1 = ent("e1", Id={"items": [{"Type": "SSN", "Value": "900"}, {"Type": "DL", "Value": "901"}]})
    e2 = ent("e2", Id={"items": [{"Type": "DL", "Value": "900"}, {"Type": "SSN", "Value": "901"}]})
    ev = evaluate(grp(rule), e1, e2)
    assert ev.matched and ev.cross_instance_attrs == ["Id"]
    e3 = ent("e3", Id={"items": [{"Type": "SSN", "Value": "900"}]})
    assert evaluate(grp(rule), e1, e3).cross_instance_attrs == []


def test_multi_operator_cross_multi():
    rule = {"and": {"multi": [{"uri": T + "MultiGroup", "attributes": [T + "FN", T + "LN"]}]},
            "comparatorClasses": {"mapping": [{"attribute": T + "MultiGroup", "class": "CrossMultiComparator"}]}}
    assert evaluate(grp(rule), ent("e1", FN="John", LN="Doe"), ent("e2", FN="Doe", LN="John")).matched
    assert not evaluate(grp(rule), ent("e1", FN="John", LN="Doe"), ent("e2", FN="Doe")).matched


def test_multiple_groups_match_same_pair(syn_engine, syn_records):
    evs = syn_engine.evaluate_all(syn_records["e01"], syn_records["e02"])
    matched = sorted(e.group_uri.rsplit("/", 1)[-1] for e in evs if e.matched)
    assert matched == ["SynExactNameDob", "SynIdFuzzyName", "SynRelevance"]


def test_non_ov_values_ignored(syn_engine, syn_groups, syn_records):
    # e01 has a non-OV FirstName 'Johnny'; only 'John' may be compared
    lf = [l for l in syn_engine.evaluate(syn_groups["SynExactNameDob"], syn_records["e01"],
                                         syn_records["e02"]).leaves() if l.attribute.endswith("FirstName")][0]
    assert "Johnny" not in lf.left


# ---------------------------------------------------------------- parser
def test_unknown_operator_and_setting_are_reported():
    g = parse_match_group({"uri": "configuration/entityTypes/T/matchGroups/G", "type": "suspect",
                           "someNewSetting": 1, "rule": {"and": {"exact": [T + "A"], "sounds": [T + "B"]}}})
    assert LOG.counts["UNSUPPORTED_OPERATOR"] == 1 and LOG.counts["UNPARSED_SETTING"] == 1
    ev = RuleEngine([g]).evaluate(g, build_record(ent("a", A="1", B="x"), g.attributes()),
                                  build_record(ent("b", A="1", B="x"), g.attributes()))
    assert ev.unsupported and not ev.matched


def test_target_rule_selection(syn_groups):
    from fpnp import synthetic
    gs = parse_match_groups(synthetic.synthetic_config(), ["Person"], "SynRelevance,SynHousehold")
    assert [g.name for g in gs] == ["SynRelevance", "SynHousehold"]


@pytest.mark.skipif(not os.path.isfile(REAL_L3), reason="tenant L3.json not present")
def test_real_configuration_inventory():
    gs = parse_match_groups(load_config(REAL_L3))
    inv = inventory(gs)
    assert len(gs) == 39
    assert set(inv["operators"]) == {"and", "or", "not", "exact", "exactOrNull", "exactOrAllNull", "fuzzy",
                                     "multi", "equals", "notEquals", "in"}
    assert LOG.counts["UNSUPPORTED_OPERATOR"] == 0
    from fpnp import comparator_registry as registry
    missing = set(inv["comparators"]) - set(registry.registered())
    assert not missing
