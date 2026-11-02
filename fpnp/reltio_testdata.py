"""
Validation dataset for the Reltio FP / FN engine - one designed case per scenario:

   1 exact matches                    v01 ~ v02   (also: 11 multiple match rules)
   2 fuzzy matches                    v03 ~ v04   Katherine / Catherine (Double Metaphone)
   3 different spellings              v05 ~ v06   Smith / Smyth, Jonathan / Jonathon
   4 null vs non-null                 v07 ~ v08   MiddleName on one side only (exactOrNull)
   5 both-null                        v09 ~ v10   Suffix missing on both (exactOrAllNull) + DoB missing on both
   6 multi-value attributes           v11 ~ v12   several phones / emails, one shared
   7 nested attributes                v09 ~ v10   Address.AddressLine1 / Zip5, Identifiers.Type / Value
   8 different DOB                    v13 ~ v14   same name, DoB one day apart
   9 different identifiers            v15 ~ v16   everything equal except NPI
  10 same name, different identifiers v17 ~ v18   common name, different NPI and DoB
  11 multiple match rules             v01 ~ v02   satisfies several rules
  12 entities with many crosswalks    v19 ~ v20   v19 has three source crosswalks
  13 already merged entities          v21 ~ v22   share a crosswalk -> excluded from FN
  14 different OV values              v23 ~ v24   equal only in non-OV values -> must NOT match on OV

Synthetic results are NOT evidence of production FP / FN rates. Entity type "HCP" (deliberately
not "Individual") so nothing can depend on a hardcoded name.
"""

import csv
import json
import os
from typing import Dict, List

from .legacy import v3

ET = "HCP"
P = f"configuration/entityTypes/{ET}/attributes/"
G = f"configuration/entityTypes/{ET}/matchGroups/"
C = "com.reltio.match.comparator."
T = "com.reltio.match.token."


def _m(pairs):
    return {"mapping": [{"attribute": P + a, "class": c} for a, c in pairs]}


def rules_config() -> dict:
    groups = [
        {"uri": G + "R1_ExactNameDobNPI", "label": "R1 Exact Name + DoB + NPI", "type": "automatic",
         "scope": "INTERNAL", "useOvOnly": "true",
         "rule": {"matchTokenClasses": _m([("FirstName", T + "ExactMatchToken"), ("LastName", T + "ExactMatchToken"),
                                           ("Identifiers/attributes/Value", T + "ExactMatchToken")]),
                  "comparatorClasses": _m([("FirstName", C + "BasicStringComparator"),
                                           ("LastName", C + "BasicStringComparator"),
                                           ("DoB", C + "BasicStringComparator"),
                                           ("Identifiers/attributes/Value", C + "BasicStringComparator")]),
                  "ignoreInToken": [P + "DoB"],
                  "and": {"exact": [P + "FirstName", P + "LastName", P + "DoB", P + "Identifiers/attributes/Value"]}}},
        {"uri": G + "R2_FuzzyNameDob", "label": "R2 Fuzzy First Name + Last Name + DoB", "type": "suspect",
         "scope": "INTERNAL", "useOvOnly": "true",
         "rule": {"matchTokenClasses": _m([("FirstName", T + "DoubleMetaphoneMatchToken"),
                                           ("LastName", T + "ExactMatchToken"), ("DoB", T + "ExactMatchToken")]),
                  "comparatorClasses": _m([("FirstName", C + "DoubleMetaphoneComparator"),
                                           ("LastName", C + "BasicStringComparator"),
                                           ("DoB", C + "BasicStringComparator"),
                                           ("MiddleName", C + "BasicStringComparator")]),
                  "and": {"fuzzy": [P + "FirstName"], "exact": [P + "LastName", P + "DoB"],
                          "exactOrNull": [P + "MiddleName"]}}},
        {"uri": G + "R3_NameAddress", "label": "R3 Name + Address", "type": "suspect",
         "scope": "INTERNAL", "useOvOnly": "true",
         "rule": {"matchTokenClasses": _m([("LastName", T + "ExactMatchToken"),
                                           ("Address/attributes/Zip5", T + "ExactMatchToken")]),
                  "comparatorClasses": _m([("FirstName", C + "JaroWinklerComparator"),
                                           ("LastName", C + "BasicStringComparator"),
                                           ("Address/attributes/AddressLine1", C + "AddressLineComparator"),
                                           ("Address/attributes/Zip5", C + "BasicStringComparator"),
                                           ("Suffix", C + "BasicStringComparator")]),
                  "ignoreInToken": [P + "FirstName", P + "Address/attributes/AddressLine1"],
                  "and": {"fuzzy": [P + "FirstName", P + "Address/attributes/AddressLine1"],
                          "exact": [P + "LastName", P + "Address/attributes/Zip5"],
                          "exactOrAllNull": [P + "Suffix"]}}},
        {"uri": G + "R4_Relevance", "label": "R4 Relevance Name/DoB/NPI/Phone", "type": "relevance_based",
         "scope": "INTERNAL", "useOvOnly": "true",
         "rule": {"matchTokenClasses": _m([("LastName", T + "DoubleMetaphoneMatchToken")]),
                  "comparatorClasses": _m([("FirstName", C + "DoubleMetaphoneComparator"),
                                           ("LastName", C + "DynamicDamerauLevenshteinDistance"),
                                           ("DoB", C + "BasicStringComparator"),
                                           ("Identifiers/attributes/Value", C + "BasicStringComparator"),
                                           ("Phone", C + "PhoneNumberComparator")]),
                  "ignoreInToken": [P + "FirstName", P + "DoB", P + "Identifiers/attributes/Value", P + "Phone"],
                  "and": {"fuzzy": [P + "FirstName", P + "LastName"], "exact": [P + "DoB"],
                          "exactOrNull": [P + "Identifiers/attributes/Value", P + "Phone"]},
                  "weights": [{"attribute": P + "FirstName", "weight": 0.2}, {"attribute": P + "LastName", "weight": 0.25},
                              {"attribute": P + "DoB", "weight": 0.25},
                              {"attribute": P + "Identifiers/attributes/Value", "weight": 0.2},
                              {"attribute": P + "Phone", "weight": 0.1}],
                  "actionThresholds": [{"type": "auto_merge", "threshold": "0.95-1.0", "label": "Auto"},
                                       {"type": "potential_match", "threshold": "0.70-0.95", "label": "Review"}]}},
        {"uri": G + "R5_NPIOnly", "label": "R5 NPI identifier", "type": "suspect", "scope": "INTERNAL",
         "useOvOnly": "true",
         "rule": {"matchTokenClasses": _m([("Identifiers/attributes/Value", T + "ExactMatchToken")]),
                  "comparatorClasses": _m([("Identifiers/attributes/Type", C + "BasicStringComparator"),
                                           ("Identifiers/attributes/Value", C + "BasicStringComparator")]),
                  "ignoreInToken": [P + "Identifiers/attributes/Type"],
                  "equals": [{"uri": P + "Identifiers/attributes/Type", "value": "NPI"}],
                  "and": {"exact": [P + "Identifiers/attributes/Type", P + "Identifiers/attributes/Value"],
                          "notExactSame": [P + "Gender"]}}},
    ]
    return {"uri": "configuration", "label": "Reltio FP/FN validation configuration",
            "entityTypes": [{"uri": f"configuration/entityTypes/{ET}", "matchGroups": groups}]}


def _v(x, ov=True):
    return {"value": x, "ov": ov}


def hcp(eid, fn=None, ln=None, dob=None, mn=None, sx=None, gender=None, npi=None, ids=(), addr=None, phones=(),
        emails=(), crosswalks=None, extra=None):
    a: Dict[str, list] = {}
    for k, v in (("FirstName", fn), ("LastName", ln), ("DoB", dob), ("MiddleName", mn), ("Suffix", sx),
                 ("Gender", gender)):
        if v is not None:
            a[k] = [_v(v)]
    idl = ([("NPI", npi)] if npi else []) + list(ids)
    if idl:
        a["Identifiers"] = [{"ov": True, "uri": f"entities/{eid}/attributes/Identifiers/i{k}",
                             "value": {"Type": [_v(t)], "Value": [_v(v)]}} for k, (t, v) in enumerate(idl)]
    if addr:
        a["Address"] = [{"ov": True, "uri": f"entities/{eid}/attributes/Address/a0",
                         "value": {"AddressLine1": [_v(addr[0])], "City": [_v(addr[1])], "Zip5": [_v(addr[2])]}}]
    if phones:
        a["Phone"] = [_v(p) for p in phones]
    if emails:
        a["Email"] = [_v(e) for e in emails]
    for k, v in (extra or {}).items():
        a.setdefault(k, []).extend(v)
    cws = crosswalks or [("CRM", eid)]
    return {"uri": f"entities/{eid}", "type": f"configuration/entityTypes/{ET}", "attributes": a,
            "createdTime": 1700000000000, "updatedTime": 1760000000000,
            "crosswalks": [{"type": f"configuration/sources/{s}", "value": v} for s, v in cws]
            + [{"type": "configuration/sources/ReltioCleanser", "value": eid}]}


def entities() -> List[dict]:
    E = [
        hcp("v01", "John", "Carter", "1970-01-01", gender="M", npi="1234567890", addr=("10 Main St", "Dallas", "75001"),
            phones=["+1 (214) 555-0101"]),
        hcp("v02", "JOHN", "carter", "1970-01-01", gender="M", npi="1234567890", addr=("10 Main Street", "Dallas", "75001"),
            phones=["214-555-0101"]),
        hcp("v03", "Katherine", "Lowe", "1980-02-02", npi="2000000001"),
        hcp("v04", "Catherine", "Lowe", "1980-02-02", npi="2000000002"),
        hcp("v05", "Jonathan", "Smith", "1975-03-03", phones=["4695550199"]),
        hcp("v06", "Jonathon", "Smyth", "1975-03-03", phones=["469-555-0199"]),
        hcp("v07", "Maria", "Gomez", "1985-04-04", mn="Luisa"),
        hcp("v08", "Maria", "Gomez", "1985-04-04"),
        hcp("v09", "Ahmed", "Khan", None, addr=("77 Lake Rd", "Austin", "73301")),
        hcp("v10", "Ahmad", "Khan", None, addr=("77 Lake Road", "Austin", "73301")),
        hcp("v11", "Grace", "Hopper", "1906-12-09", phones=["2125550001", "2125550002"],
            emails=["grace@navy.mil", "gh@yale.edu"]),
        hcp("v12", "Grace", "Hopper", "1906-12-09", phones=["(212) 555-0002"], emails=["gh@yale.edu"]),
        hcp("v13", "Emily", "Stone", "1990-05-05", npi="3000000001"),
        hcp("v14", "Emily", "Stone", "1990-05-06", npi="3000000002"),
        hcp("v15", "Raj", "Patel", "1965-06-06", npi="4000000001", addr=("5 Oak Ave", "Plano", "75024")),
        hcp("v16", "Raj", "Patel", "1965-06-06", npi="4000000009", addr=("5 Oak Ave", "Plano", "75024")),
        hcp("v17", "Michael", "Brown", "1972-07-07", npi="5000000001"),
        hcp("v18", "Michael", "Brown", "1981-08-08", npi="5999999999"),
        hcp("v19", "Olivia", "Reed", "1988-09-09", npi="6000000001",
            crosswalks=[("CRM", "v19"), ("NPPES", "N6000000001"), ("Veeva", "VV-19")]),
        hcp("v20", "Olivia", "Reed", "1988-09-09", npi="6000000001", crosswalks=[("Claims", "C-20")]),
        hcp("v21", "Henry", "Ford", "1963-07-30", npi="7000000001", crosswalks=[("CRM", "SHARED-21")]),
        hcp("v22", "Henry", "Ford", "1963-07-30", npi="7000000001", crosswalks=[("Veeva", "V-22"), ("CRM", "SHARED-21")]),
        hcp("v23", "Robert", "King", "1977-10-10", extra={"FirstName": [_v("Bob", ov=False)]}),
        hcp("v24", "Bob", "King", "1977-10-10", extra={"FirstName": [_v("Robert", ov=False)]}),
    ]
    filler = ["Zora Neale", "Yuri Gagar", "Xena Warri", "Wade Wilson", "Uma Turman", "Tara Reid", "Seth Rogen",
              "Rosa Parks", "Quincy Jones", "Pia Mia"]
    for k, n in enumerate(filler, 30):
        fn, ln = n.split()
        E.append(hcp(f"v{k}", fn, ln, f"19{50 + k % 40}-0{1 + k % 9}-1{k % 9}", npi=f"9{k:09d}"))
    E.append(dict(E[0]))                       # v01 exported twice (duplicate handling)
    return E


# pairs Reltio returned (Potential Match Report) - illustrative
REPORT = [("v01", "v02", "R1_ExactNameDobNPI", ""), ("v01", "v02", "R2_FuzzyNameDob", ""),
          ("v03", "v04", "R2_FuzzyNameDob", ""), ("v05", "v06", "R4_Relevance", "0.86"),
          ("v13", "v14", "R4_Relevance", "0.72"), ("v17", "v18", "R4_Relevance", "0.71"),
          ("v09", "v10", "R3_NameAddress", ""),
          # Reltio returned v09/v10 under R2 although exact(DoB) is null on BOTH records - the tenant behaviour
          # the null-semantics probe is designed to detect
          ("v09", "v10", "R2_FuzzyNameDob", "")]
STEWARD = [("v01", "v02", "MATCH"), ("v17", "v18", "NOT_A_MATCH"), ("v11", "v12", "MATCH")]
KNOWN = [("v07", "v08", "linked by data steward (not a match flag reviewed)")]


def build(out_dir: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    cfg = rules_config()
    paths = {"config": os.path.join(out_dir, "validation_rules.json"),
             "entities": os.path.join(out_dir, "validation_entities.json"),
             "report": os.path.join(out_dir, "validation_potential_matches.csv"),
             "steward": os.path.join(out_dir, "validation_steward_decisions.csv"),
             "known": os.path.join(out_dir, "validation_known_matches.csv")}
    with open(paths["config"], "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1)
    ents = entities()
    with open(paths["entities"], "w", encoding="utf-8") as fh:     # JSON array, as Reltio exports it
        json.dump(ents, fh, indent=0)
    from .rule_config_parser import parse_match_groups
    groups = {g.name: g for g in parse_match_groups(cfg)}
    by_id = {e["uri"].split("/")[-1]: e for e in ents}
    union: List[str] = []
    rows = []
    for a, b, rule, rel in REPORT:          # written with the existing extractor's own functions
        g = groups[rule]
        attrs = v3.rule_attributes(g.raw, ET)
        union.extend(x for x in attrs if x not in union)
        src, mat = v3.extract_values(by_id[a], attrs, True), v3.extract_values(by_id[b], attrs, True)
        row = {"Rule": g.uri, "RuleLabel": g.label, "ReltioRelevance": rel,
               "ReltioMatchAction": "potential_match" if rel else ""}
        for k in src:
            row[f"src_{k}"], row[f"match_{k}"] = src[k], mat.get(k, "")
        rows.append(row)
    v3.write_csv(paths["report"], v3.csv_header_for(union), rows, True, append=False)
    for key, data, header in (("steward", STEWARD, ["EntityURI1", "EntityURI2", "Decision"]),
                              ("known", KNOWN, ["EntityURI1", "EntityURI2", "Reason"])):
        with open(paths[key], "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for a, b, d in data:
                w.writerow([f"entities/{a}", f"entities/{b}", d])
    return paths
