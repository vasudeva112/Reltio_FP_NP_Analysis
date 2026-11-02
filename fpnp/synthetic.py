"""
Synthetic dataset with explicitly designed outcomes (used by tests and the sample report).

Synthetic results are NOT evidence of production FP/FN rates.

Entity type 'Person' (deliberately not 'Individual') and four match groups:
  SynExactNameDob  suspect          exact(FirstName, LastName, DoB) exactOrNull(MiddleName);
                                    negativeRule: both Status = DECEASED
  SynIdFuzzyName   suspect          fuzzy(FirstName) exact(LastName, Identifiers.Type, Identifiers.Value)
                                    in(Identifiers.Type: SSN, ITN); FirstName token = ExactMatchToken
  SynRelevance     relevance_based  fuzzy(FirstName, LastName) exact(DoB) exactOrNull(Suffix)
                                    weights .3/.3/.3/.1, potential_match 0.7-1
  SynHousehold     suspect          exact(LastName, Address.AddressLine1, Address.Zip5)
                                    exactOrAllNull(MiddleName) not(exact(FirstName))
"""

import csv
import json
import os
from typing import Dict, List

from .legacy import v3

ET = "Person"
P = f"configuration/entityTypes/{ET}/attributes/"
G = f"configuration/entityTypes/{ET}/matchGroups/"
BASIC = "com.reltio.match.comparator.BasicStringComparator"
DM = "com.reltio.match.comparator.DoubleMetaphoneComparator"
EXACT_T = "com.reltio.match.token.ExactMatchToken"
DM_T = "com.reltio.match.token.DoubleMetaphoneMatchToken"


def _map(pairs):
    return {"mapping": [{"attribute": P + a, "class": c} for a, c in pairs]}


def synthetic_config() -> dict:
    groups = [
        {
            "uri": G + "SynExactNameDob", "label": "SynExactNameDob: Exact(FN,LN,DOB) EON(MN)", "type": "suspect",
            "scope": "INTERNAL", "useOvOnly": "true",
            "rule": {
                "matchTokenClasses": _map([("FirstName", EXACT_T), ("LastName", EXACT_T), ("DoB", EXACT_T)]),
                "comparatorClasses": _map([("FirstName", BASIC), ("LastName", BASIC), ("DoB", BASIC),
                                           ("MiddleName", BASIC)]),
                "and": {"exact": [P + "FirstName", P + "LastName", P + "DoB"], "exactOrNull": [P + "MiddleName"]},
            },
            "negativeRule": {"and": {"equals": [{"uri": P + "Status", "values": ["DECEASED"]}]}},
            "scoreStandalone": 0, "scoreIncremental": 0,
        },
        {
            "uri": G + "SynIdFuzzyName", "label": "SynIdFuzzyName: Fuzzy(FN) Exact(LN,ID)", "type": "suspect",
            "scope": "INTERNAL", "useOvOnly": "true",
            "rule": {
                "ignoreInToken": [P + "Identifiers/attributes/Type"],
                "matchTokenClasses": _map([("FirstName", EXACT_T), ("LastName", EXACT_T),
                                           ("Identifiers/attributes/Value", EXACT_T)]),
                "comparatorClasses": _map([("FirstName", DM), ("LastName", BASIC),
                                           ("Identifiers/attributes/Type", BASIC),
                                           ("Identifiers/attributes/Value", BASIC)]),
                "in": [{"values": ["SSN", "ITN"], "uri": P + "Identifiers/attributes/Type"}],
                "and": {"fuzzy": [P + "FirstName"],
                        "exact": [P + "LastName", P + "Identifiers/attributes/Type", P + "Identifiers/attributes/Value"]},
            },
            "scoreStandalone": 0, "scoreIncremental": 0,
        },
        {
            "uri": G + "SynRelevance", "label": "SynRelevance: Fuzzy(FN,LN) Exact(DOB) EON(SX)",
            "type": "relevance_based", "scope": "INTERNAL", "useOvOnly": "true",
            "rule": {
                "ignoreInToken": [P + "DoB"],
                "matchTokenClasses": _map([("FirstName", EXACT_T), ("LastName", DM_T)]),
                "comparatorClasses": _map([("FirstName", DM), ("LastName", DM), ("DoB", BASIC), ("Suffix", BASIC)]),
                "and": {"fuzzy": [P + "FirstName", P + "LastName"], "exact": [P + "DoB"],
                        "exactOrNull": [P + "Suffix"]},
                "weights": [{"attribute": P + "FirstName", "weight": 0.3}, {"attribute": P + "LastName", "weight": 0.3},
                            {"attribute": P + "DoB", "weight": 0.3}, {"attribute": P + "Suffix", "weight": 0.1}],
                "actionThresholds": [{"type": "potential_match", "threshold": "0.7-1", "label": "Synthetic"}],
            },
            "scoreStandalone": 0, "scoreIncremental": 0,
        },
        {
            "uri": G + "SynHousehold", "label": "SynHousehold: Exact(LN,AD1,ZIP) EOAN(MN) NOT(FN)",
            "type": "suspect", "scope": "INTERNAL", "useOvOnly": "true",
            "rule": {
                "matchTokenClasses": _map([("LastName", EXACT_T), ("Address/attributes/AddressLine1", EXACT_T),
                                           ("Address/attributes/Zip5", EXACT_T)]),
                "comparatorClasses": _map([("LastName", BASIC), ("Address/attributes/AddressLine1", BASIC),
                                           ("Address/attributes/Zip5", BASIC), ("MiddleName", BASIC),
                                           ("FirstName", BASIC)]),
                "and": {"exact": [P + "LastName", P + "Address/attributes/AddressLine1", P + "Address/attributes/Zip5"],
                        "exactOrAllNull": [P + "MiddleName"],
                        "not": {"exact": [P + "FirstName"]}},
            },
            "scoreStandalone": 0, "scoreIncremental": 0,
        },
    ]
    return {"uri": "configuration", "label": "FPNP synthetic configuration",
            "entityTypes": [{"uri": f"configuration/entityTypes/{ET}", "matchGroups": groups}]}


def _v(x, ov=True):
    return {"value": x, "ov": ov}


def person(eid, fn=None, ln=None, dob=None, mn=None, sx=None, ids=(), addr=None, status=None, extra=None):
    a: Dict[str, list] = {}
    if fn is not None:
        a["FirstName"] = [_v(fn)]
    if ln is not None:
        a["LastName"] = [_v(ln)]
    if dob is not None:
        a["DoB"] = [_v(dob)]
    if mn is not None:
        a["MiddleName"] = [_v(mn)] if mn != "<null>" else None
    if sx is not None:
        a["Suffix"] = [_v(sx)]
    if status is not None:
        a["Status"] = [_v(status)]
    if ids:
        a["Identifiers"] = [{"ov": True, "uri": f"entities/{eid}/attributes/Identifiers/i{k}",
                             "value": {"Type": [_v(t)], "Value": [_v(v)]}} for k, (t, v) in enumerate(ids)]
    if addr:
        a["Address"] = [{"ov": True, "uri": f"entities/{eid}/attributes/Address/a0",
                         "value": {"AddressLine1": [_v(addr[0])], "Zip5": [_v(addr[1])]}}]
    for k, v in (extra or {}).items():
        a.setdefault(k, []).extend(v)
    return {"uri": f"entities/{eid}", "type": f"configuration/entityTypes/{ET}", "attributes": a,
            "crosswalks": [{"type": "configuration/sources/SYN", "value": eid},
                           {"type": "configuration/sources/ReltioCleanser", "value": eid}]}


def synthetic_entities() -> List[dict]:
    E = [
        # genuine duplicate (inconsistent case/spacing, one-sided middle name); non-OV value must be ignored
        person("e01", "John", "Smith", "1980-01-01", mn="A", ids=[("SSN", "111")], addr=("1 Main St", "30301"),
               extra={"FirstName": [_v("Johnny", ov=False)]}),
        person("e02", "john ", "SMITH", "1980-01-01", ids=[("SSN", "111")], addr=("1 Main St", "30301")),
        # similar names, different people (DoB/ids differ) - relevance rule still matches at 0.7
        person("e03", "Jon", "Smith", "1975-03-03", ids=[("SSN", "333")]),
        person("e04", "Jon", "Smyth", "1975-03-04", ids=[("SSN", "444")]),
        # same name/DoB, different identifiers
        person("e05", "Mary", "Jones", "1990-05-05", ids=[("SSN", "555")]),
        person("e06", "Mary", "Jones", "1990-05-05", ids=[("SSN", "556")]),
        # null middle name on one side: exactOrNull passes -> not in Reltio output -> FN
        person("e07", "Peter", "Parker", "1985-07-07", mn="B", addr=("9 Elm Rd", "10001")),
        person("e08", "Peter", "Parker", "1985-07-07", mn="<null>", addr=("22 Oak Ave", "10002")),
        # formatting differences
        person("e09", " JOHN ", "DOE", "1970-09-09"),
        person("e10", "john", "doe ", "1970-09-09"),
        # only SynIdFuzzyName matches; FirstName tokens differ (ExactMatchToken) -> tokenization miss
        person("e11", "Jon", "Kent", "1960-01-01", sx="Sr", ids=[("SSN", "777")], addr=("5 Hill Rd", "20001")),
        person("e12", "John", "Kent", "1961-02-02", sx="Jr", ids=[("SSN", "777")], addr=("8 Lake Dr", "20002")),
        # negative rule: both deceased
        person("e13", "Ann", "Grey", "1950-05-05", status="DECEASED"),
        person("e14", "Ann", "Grey", "1950-05-05", status="DECEASED"),
        # household: both middle names null (exactOrAllNull passes) vs one-sided null (fails)
        person("e15", "Anna", "Lee", "1991-01-01", addr=("7 Pine Way", "40001")),
        person("e16", "Ben", "Lee", "1992-02-02", addr=("7 Pine Way", "40001")),
        person("e17", "Carl", "Lee", "1993-03-03", mn="C", addr=("7 Pine Way", "40001")),
        # different identifier types on the same nested instance (cross-instance check)
        person("e18", "Rita", "Moss", "1966-06-06", ids=[("SSN", "900"), ("DLIC", "901")]),
        person("e19", "Rita", "Moss", "1966-06-07", ids=[("DLIC", "900"), ("SSN", "901")]),
    ]
    names = ["Zed Quill", "Yara Vance", "Xavi Ortiz", "Wynn Cole", "Uma Hart", "Theo Banks", "Sara Pike",
             "Ralf Nunez", "Quin Drake", "Paul Ives", "Olga Frost", "Nina Webb"]
    for k, n in enumerate(names, 20):
        fn, ln = n.split()
        E.append(person(f"e{k}", fn, ln, f"19{40 + k}-0{1 + k % 9}-1{k % 9}", ids=[("SSN", f"8{k:03d}")]))
    E.append(dict(E[1]))           # e02 exported twice -> duplicate handling
    return E


# Reltio output (pairs + rule) and steward decisions for the synthetic data
REPORT_PAIRS = [
    ("e01", "e02", "SynExactNameDob"),
    ("e01", "e02", "SynIdFuzzyName"),
    ("e02", "e01", "SynRelevance"),       # reversed order on purpose
    ("e03", "e04", "SynRelevance"),
    ("e05", "e06", "SynExactNameDob"),
    ("e09", "e10", "SynExactNameDob"),
    ("e15", "e16", "SynHousehold"),
    ("e05", "e20", "SynRelevance"),       # Python will not reproduce this one
    ("e18", "e19", "SynIdFuzzyName"),
]
STEWARD = [
    ("e01", "e02", "MATCH"),
    ("e03", "e04", "NOT_A_MATCH"),
    ("e07", "e08", "MATCH"),
]


def build(out_dir: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    cfg_path = os.path.join(out_dir, "synthetic_L3.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(synthetic_config(), fh, indent=1)

    ents = synthetic_entities()
    ent_path = os.path.join(out_dir, "synthetic_entities.jsonl")
    with open(ent_path, "w", encoding="utf-8") as fh:
        for e in ents:
            fh.write(json.dumps(e) + "\n")

    # Potential Match Report written with the existing extractor's own functions
    from .rule_config_parser import parse_match_groups
    groups = {g.name: g for g in parse_match_groups(synthetic_config())}
    by_id = {e["uri"].split("/")[-1]: e for e in ents}
    union: List[str] = []
    rows = []
    for a, b, rule in REPORT_PAIRS:
        g = groups[rule]
        paths = v3.rule_attributes(g.raw, ET)
        union.extend(p for p in paths if p not in union)
        src = v3.extract_values(by_id[a], paths, True)
        mat = v3.extract_values(by_id[b], paths, True)
        row = {"Rule": g.uri, "RuleLabel": g.label}
        for k in src:
            row[f"src_{k}"] = src[k]
            row[f"match_{k}"] = mat.get(k, "")
        rows.append(row)
    rep_path = os.path.join(out_dir, "synthetic_potential_matches.csv")
    v3.write_csv(rep_path, v3.csv_header_for(union), rows, True, append=False)

    st_path = os.path.join(out_dir, "synthetic_steward_decisions.csv")
    with open(st_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["EntityURI1", "EntityURI2", "Decision", "Reviewer", "Comment"])
        for a, b, d in STEWARD:
            w.writerow([f"entities/{a}", f"entities/{b}", d, "synthetic", "designed outcome"])
    return {"config": cfg_path, "entities": ent_path, "report": rep_path, "steward": st_path}
