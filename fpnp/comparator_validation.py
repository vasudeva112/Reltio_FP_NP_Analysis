"""
Controlled validation cases for operators, comparators and token classes.

Expected values are written by hand from the documented Reltio semantics (or from the
labelled assumption where documentation is silent). The same catalogue drives the
unit tests and Sheet 6 of the workbook. A "Reltio result" column is filled only from
tenant verification results; until then every row says "not tested on tenant".
"""

from typing import Dict, List, Optional

from . import comparator_registry as registry
from .attribute_extractor import extract
from .cleansers import PreparedValues
from .comparators.base import EngineContext
from .match_tokens import make_token_class
from .operators import OPERATOR_DOCS, evaluate_attribute_op, evaluate_constraint

MISSING = object()          # attribute key absent


def attr_json(v):
    if v is MISSING:
        return {}
    if v is None:
        return {"A": None}
    if isinstance(v, list):
        return {"A": [{"value": x, "ov": True} for x in v]}
    return {"A": [{"value": v, "ov": True}]}


def prepared(v, ctx: EngineContext) -> PreparedValues:
    av = extract(attr_json(v), ["A"], True, ctx.treat_blank_as_null)
    return PreparedValues(av.state, av.leaves, tuple(l.value for l in av.leaves))


def show(v) -> str:
    if v is MISSING:
        return "<missing attribute>"
    if v is None:
        return "<null>"
    if isinstance(v, list):
        return "[" + ", ".join(show(x) for x in v) + "]"
    return repr(v)


NULL_CASES = [
    ("both populated, equal", "abc", "abc"),
    ("both populated, different", "abc", "xyz"),
    ("left null, right populated", None, "abc"),
    ("left populated, right null", "abc", None),
    ("both null", None, None),
    ("attribute missing (left)", MISSING, "abc"),
    ("attribute missing (both)", MISSING, MISSING),
    ("empty string vs populated", "", "abc"),
    ("whitespace-only vs populated", "   ", "abc"),
    ("empty collection vs populated", [], "abc"),
    ("multi-value incl. null/empty, one equal", ["abc", None, ""], ["abc"]),
    ("multi-value only null/empty vs populated", ["", None], ["abc"]),
    ("case and surrounding whitespace differ", " ABC ", "abc"),
]
#            eq     diff   Lnull  Rnull  bnull  Lmiss  bmiss  empty  ws     []     multi  m-null case
EXPECTED_OPS = {
    "exact":          [True, False, False, False, False, False, False, False, False, False, True, False, True],
    "fuzzy":          [True, False, False, False, False, False, False, False, False, False, True, False, True],
    "exactOrNull":    [True, False, True,  True,  True,  True,  True,  True,  True,  True,  True, True,  True],
    "exactOrAllNull": [True, False, False, False, True,  False, True,  False, False, False, True, False, True],
    "notExactSame":   [False, True, True,  True,  False, True,  False, True,  True,  True,  False, True, False],
}

CONSTRAINT_CASES = [
    # op, values, left, right, expected
    ("equals", ["Y"], "Y", "y", True),
    ("equals", ["Y"], "Y", "N", False),
    ("equals", ["Y"], "Y", None, False),
    ("in", ["SSN", "ITN", "EIN"], ["SSN", "DLIC"], "itn", True),
    ("in", ["SSN", "ITN", "EIN"], "DLIC", "SSN", False),
    ("notEquals", ["PURGED"], "ACTIVE", None, True),
    ("notEquals", ["PURGED"], "purged", "ACTIVE", False),
    ("notEquals", ["X1"], ["X1", "X2"], "X3", False),     # any listed value fails (assumption)
]

COMPARATOR_CASES = [
    # class, params, left, right, expected equal, note
    ("BasicStringComparator", {}, "Smith", "smith", True, "case-insensitive (assumption)"),
    ("BasicStringComparator", {}, " Smith ", "Smith", True, "trimmed"),
    ("BasicStringComparator", {}, "Smith", "Smyth", False, ""),
    ("DoubleMetaphoneComparator", {}, "Catherine", "Kathryn", True, "same primary code"),
    ("DoubleMetaphoneComparator", {}, "Smith", "Schmidt", True, "secondary/primary cross match"),
    ("DoubleMetaphoneComparator", {}, "Jon", "John", True, ""),
    ("DoubleMetaphoneComparator", {}, "Billyjean", "Waiman", False, ""),
    ("DynamicDamerauLevenshteinDistance", {}, "Jonathan", "Jonathon", True, "1 edit, length 8 allows 2"),
    ("DynamicDamerauLevenshteinDistance", {}, "Smith", "Smiht", True, "transposition = 1 edit"),
    ("DynamicDamerauLevenshteinDistance", {}, "3rd ave", "Third ave", False, "Reltio support example"),
    ("DynamicDamerauLevenshteinDistance", {}, "Ann", "Bob", False, ""),
    ("JaroWinklerComparator", {"similarityThreshold": "0.95"}, "Martha", "Marhta", True, "JW 0.961"),
    ("JaroWinklerComparator", {"similarityThreshold": "0.95"}, "Dwayne", "Duane", False, "JW 0.84"),
    ("JaroWinklerComparator", {}, "Dixon", "Dicksonx", False, "JW 0.813 < default 0.9"),
    ("PhoneNumberComparator", {}, "(818)777-09876", "818-777-0987", False, "documented example"),
    ("PhoneNumberComparator", {}, "+0177(818)777-0987", "818-7770987", True, "documented example"),
    ("PhoneNumberComparator", {}, "123456789", "123456789", False, "fewer than 10 digits (e.g. SSN)"),
    ("AddressLineComparator", {}, "4627 PALUSTRINE DR", "4627 Palustrine Drive", True, "noise words removed"),
    ("AddressLineComparator", {}, "PO Box 12", "P.O. BOX 12", True, "PO box rule"),
    ("AddressLineComparator", {}, "12 Main St", "14 Oak Ave", False, ""),
    ("RangeNumericComparator", {"threshold": "1"}, "1990", "1991", True, ""),
    ("RangeNumericComparator", {"threshold": "1"}, "1990", "1992", False, ""),
    ("CustomComparator", {"groups": [
        {"classParams": {"threshold": "1"}, "pattern": "\\d{4}+", "className": "RangeNumericComparator"},
        {"classParams": {"threshold": "1"}, "pattern": "\\-(\\d{2})\\-", "className": "RangeNumericComparator"},
        {"classParams": {"threshold": "1"}, "pattern": "\\-(\\d{2})$", "className": "RangeNumericComparator"}]},
     "1980-05-10", "1981-06-11", True, "tenant DoB config: each part +/-1"),
    ("CustomComparator", {"groups": [
        {"classParams": {"threshold": "1"}, "pattern": "\\d{4}+", "className": "RangeNumericComparator"},
        {"classParams": {"threshold": "1"}, "pattern": "\\-(\\d{2})\\-", "className": "RangeNumericComparator"},
        {"classParams": {"threshold": "1"}, "pattern": "\\-(\\d{2})$", "className": "RangeNumericComparator"}]},
     "1980-05-10", "1980-05-13", False, "day differs by 3"),
]

TOKEN_CASES = [
    ("ExactMatchToken", {}, " Smith ", {"smith"}),
    ("DoubleMetaphoneMatchToken", {}, "Smith", {"SM0", "XMT"}),
    ("PhoneNumberMatchToken", {}, "+1 (818) 777-0987", {"8187770987"}),
    ("PhoneNumberMatchToken", {}, "777-0987", set()),
    ("AddressLineMatchToken", {}, "4627 Palustrine Drive", {"palustrine"}),
]


def run_catalogue(ctx: Optional[EngineContext] = None, tenant_results: Optional[Dict[str, str]] = None) -> List[dict]:
    ctx = ctx or EngineContext()
    tenant_results = tenant_results or {}
    rows: List[dict] = []

    def add(subject, kind, case, left, right, params, expected, got, rel, status, note=""):
        key = f"{subject}|{case}"
        rows.append({
            "Subject": subject, "Kind": kind, "TestCase": case, "Left": left, "Right": right,
            "Parameters": params, "Expected": expected, "PythonResult": got,
            "PythonRelevance": rel, "ReltioResult": tenant_results.get(key, "not tested on tenant"),
            "VerificationStatus": status, "Outcome": "PASS" if expected == got else "FAIL", "Note": note,
        })

    basic = registry.create("BasicStringComparator", {}, ctx)
    for op, exp in EXPECTED_OPS.items():
        for (case, l, r), e in zip(NULL_CASES, exp):
            res = evaluate_attribute_op(op, "A", basic, ctx, prepared(l, ctx), prepared(r, ctx))
            add(op, "operator", case, show(l), show(r), "comparator=BasicStringComparator", e, res.passed,
                round(res.relevance, 4), OPERATOR_DOCS[op]["status"], OPERATOR_DOCS[op]["nulls"])
    for op, vals, l, r, e in CONSTRAINT_CASES:
        res = evaluate_constraint(op, "A", vals, ctx, prepared(l, ctx), prepared(r, ctx))
        add(op, "operator", f"{op} {vals}", show(l), show(r), "", e, res.passed, None,
            OPERATOR_DOCS[op]["status"], OPERATOR_DOCS[op]["desc"])
    for cls, params, l, r, e, note in COMPARATOR_CASES:
        cmp = registry.create(cls, params, ctx)
        res = cmp.compare(l, r)
        add(cls, "comparator", f"{l!r} vs {r!r}", l, r, str(params) if params else "", e, res.equal,
            round(res.relevance, 4), cmp.status, f"{note} {res.detail}".strip())
    multi = registry.create("CrossMultiComparator", {}, ctx)
    dm = registry.create("DoubleMetaphoneComparator", {}, ctx)
    res = multi.compare_groups([["John"], ["Doe"]], [["Doe"], ["John"]], [dm, dm])
    add("CrossMultiComparator", "comparator", "FirstName/LastName swapped", "John | Doe", "Doe | John",
        "members=DoubleMetaphoneComparator", True, res.equal, round(res.relevance, 4), multi.status, res.detail)
    for cls, params, value, expected in TOKEN_CASES:
        tc = make_token_class(cls, params, ctx)
        got = tc.tokens(value)
        add(cls, "token class", f"tokens of {value!r}", value, "", "", sorted(expected), sorted(got), None,
            tc.STATUS, tc.DOC)
    return rows


def documentation_rows() -> List[dict]:
    """One row per implemented operator / comparator / token class (README + workbook)."""
    from .match_tokens import TOKEN_REGISTRY
    rows = []
    for op, d in OPERATOR_DOCS.items():
        rows.append({"Kind": "operator", "ReltioName": op, "Status": d["status"], "NullBehaviour": d["nulls"],
                     "Description": d["desc"]})
    for name, cls in sorted(registry.registered().items()):
        doc = cls.DOC
        rows.append({"Kind": "comparator", "ReltioName": name, "Status": cls.STATUS,
                     "Parameters": doc.get("parameters", ""), "InputTypes": doc.get("input_types", ""),
                     "NullBehaviour": doc.get("null_behaviour", ""), "CaseWhitespace": doc.get("case_whitespace", ""),
                     "Normalisation": doc.get("normalisation", ""), "MultiValue": doc.get("multi_value", ""),
                     "Limitations": doc.get("limitations", ""), "DefaultTokenClass": cls.DEFAULT_TOKEN_CLASS})
    for name, cls in sorted(TOKEN_REGISTRY.items()):
        rows.append({"Kind": "token class", "ReltioName": name, "Status": cls.STATUS, "Description": cls.DOC})
    return rows
