"""
Match-rule configuration loading, catalogue and comparison.

Builds on rule_config_parser (which parses one match group into the rule tree, comparator /
token mappings, cleansers, nullValues, weights and thresholds). This module adds:

  load_rule_config(source)   accepts every common export shape and returns a tenant-style
                             {"entityTypes": [...]} document:
                               * full tenant / L3 configuration          {"entityTypes": [...]}
                               * one entity type                         {"uri": ".../entityTypes/X", "matchGroups": [...]}
                               * {"matchGroups": [...]} or a bare list of match groups
                               * a JSON string, bytes, a dict or a file path
  entity_types(config)       entity types with their match-group count
  rule_catalog(groups)       one row per rule (condition, attributes, comparators, tokens, cleansers, ...)
  attribute_catalog(groups)  one row per rule x attribute (operator, comparator + params, token class,
                             weight, normalisation, null values, fidelity)
  compare_configs(a, b)      source vs target configuration differences, rule by rule

Nothing about entity types, rules or attributes is hardcoded.
"""

import json
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Union

from .. import comparator_registry as registry
from ..comparators.base import APPROXIMATION, DOCUMENTED, UNSUPPORTED
from ..match_tokens import TOKEN_REGISTRY
from ..operators import OPERATOR_DOCS
from ..rule_config_parser import (CONSTRAINT_OPS, MatchGroup, entity_type_of, parse_match_groups, short_attr,
                                  short_class)


class ConfigError(ValueError):
    pass


def _as_document(source) -> object:
    if isinstance(source, (dict, list)):
        return source
    if isinstance(source, bytes):
        source = source.decode("utf-8-sig")
    if isinstance(source, str):
        s = source.strip()
        if s.startswith(("{", "[")):
            return json.loads(s)
        if os.path.isfile(source):
            with open(source, encoding="utf-8-sig") as fh:
                return json.load(fh)
        raise ConfigError(f"Match-rule configuration not found: {source}")
    if hasattr(source, "read"):
        data = source.read()
        return _as_document(data)
    raise ConfigError(f"Unsupported configuration input: {type(source).__name__}")


def load_rule_config(source) -> dict:
    """Normalises any supported shape to {"entityTypes": [{"uri": ..., "matchGroups": [...]}, ...]}."""
    doc = _as_document(source)
    if isinstance(doc, dict) and isinstance(doc.get("entityTypes"), list):
        return doc
    groups: List[dict] = []
    if isinstance(doc, list):
        groups = [g for g in doc if isinstance(g, dict)]
    elif isinstance(doc, dict) and isinstance(doc.get("matchGroups"), list):
        if str(doc.get("uri", "")).startswith("configuration/entityTypes/"):
            return {"entityTypes": [doc]}
        groups = doc["matchGroups"]
    elif isinstance(doc, dict) and "rule" in doc and "uri" in doc:
        groups = [doc]
    if not groups:
        raise ConfigError("No match groups found: expected a tenant configuration with entityTypes[].matchGroups, "
                          "an entity type with matchGroups, or a list of match groups")
    by_type: Dict[str, list] = defaultdict(list)
    for g in groups:
        et = entity_type_of(g.get("uri", ""))
        if not et:
            raise ConfigError(f"Match group uri does not name an entity type: {g.get('uri')!r}")
        by_type[et].append(g)
    return {"entityTypes": [{"uri": f"configuration/entityTypes/{et}", "matchGroups": gs}
                            for et, gs in by_type.items()]}


def entity_types(config: dict) -> List[dict]:
    out = []
    for et in config.get("entityTypes", []) or []:
        gs = et.get("matchGroups") or []
        out.append({"EntityType": entity_type_of(et.get("uri", "")), "MatchGroups": len(gs),
                    "Types": ", ".join(sorted({g.get("type", "") for g in gs}))})
    return out


def load_groups(source, entity_types_: Optional[Sequence[str]] = None, target_rule: str = "") -> List[MatchGroup]:
    return parse_match_groups(load_rule_config(source), list(entity_types_ or []), target_rule)


# ----------------------------------------------------------------------
def _fidelity(statuses: Iterable[str]) -> str:
    s = set(statuses)
    if UNSUPPORTED in s:
        return UNSUPPORTED
    return APPROXIMATION if APPROXIMATION in s else DOCUMENTED


def _cmp_info(g: MatchGroup, attr: str, default: str = "BasicStringComparator"):
    m = g.comparators.get(attr)
    name = m.short if m else default
    cls = registry.registered().get(name)
    status = getattr(cls, "STATUS", UNSUPPORTED) if cls else UNSUPPORTED
    params = json.dumps(m.params) if m and m.params else ""
    return name, params, status, (m is None)


def _op_name(label: str) -> str:
    """'negativeRule: NOT exact' -> 'exact'; 'in ['SSN']' -> 'in'; 'multi(Names)' -> 'multi'."""
    x = label.replace("negativeRule: ", "").replace("NOT ", "")
    return "multi" if x.startswith("multi(") else x.split()[0]


def attribute_catalog(groups: Sequence[MatchGroup]) -> List[dict]:
    rows = []
    for order, g in enumerate(groups, 1):
        ops: Dict[str, List[str]] = defaultdict(list)
        neg_ops: Dict[str, List[str]] = defaultdict(list)
        for root, target in ((g.root, ops), (g.negative_root, neg_ops)):
            if root is None:
                continue
            stack = [(root, False)]
            while stack:
                n, under_not = stack.pop()
                if n.kind == "op":
                    label = n.op if not under_not else f"NOT {n.op}"
                    if n.op in CONSTRAINT_OPS:
                        label += f" {n.values}"
                    if n.op == "multi":
                        for a in n.attributes:
                            target[a].append(f"multi({short_attr(n.attribute)})")
                    else:
                        target[n.attribute].append(label)
                for c in reversed(n.children):         # stack: reversed keeps configuration order
                    stack.append((c, under_not or n.kind == "not"))
        attrs = list(dict.fromkeys(list(ops) + list(neg_ops)))
        for a in attrs:
            name, params, status, defaulted = _cmp_info(g, a)
            tok = g.token_classes.get(a)
            tok_name = tok.short if tok else (short_class(g.group_token_class) if g.group_token_class else "")
            op_list = ops.get(a, []) + [f"negativeRule: {x}" for x in neg_ops.get(a, [])]
            op_names = [_op_name(o) for o in op_list]
            op_status = _fidelity(OPERATOR_DOCS.get(o, {}).get("status", APPROXIMATION) for o in op_names)
            only_filter = all(o in CONSTRAINT_OPS for o in op_names)
            rows.append({
                "EntityType": g.entity_type, "RuleOrder": order, "MatchRule": g.name, "MatchRuleURI": g.uri,
                "RuleType": g.type, "Attribute": short_attr(a), "AttributeURI": a,
                "Operators": ", ".join(op_list),
                "Comparator": "(filter only)" if only_filter else name + (" (default)" if defaulted else ""),
                "ComparatorParams": "" if only_filter else params,
                "ComparatorFidelity": "" if only_filter else status,
                "OperatorFidelity": op_status,
                "MatchTokenClass": tok_name + (f" {json.dumps(tok.params)}" if tok and tok.params else ""),
                "IgnoreInToken": a in g.ignore_in_token,
                "Weight": g.weight_for(a) if g.is_relevance else "",
                "Cleansers": ", ".join(sorted({short_class(c.adapter) for c in g.cleansers if c.attribute == a})),
                "NullValues": ", ".join(sorted(g.null_values.get(a, set()))),
            })
    return rows


def rule_catalog(groups: Sequence[MatchGroup]) -> List[dict]:
    rows = []
    attr_rows = attribute_catalog(groups)
    by_rule: Dict[str, List[dict]] = defaultdict(list)
    for r in attr_rows:
        by_rule[r["MatchRuleURI"]].append(r)
    for order, g in enumerate(groups, 1):
        sm = g.summary()
        ar = by_rule[g.uri]
        fid = _fidelity([r["ComparatorFidelity"] for r in ar if r["ComparatorFidelity"]]
                        + [r["OperatorFidelity"] for r in ar])
        tok_status = _fidelity(getattr(TOKEN_REGISTRY.get(m.short), "STATUS", UNSUPPORTED)
                               for m in g.token_classes.values()) if g.token_classes else ""
        rows.append({
            "EntityType": g.entity_type, "RuleOrder": order, "MatchRule": g.name, "Label": g.label,
            "MatchRuleURI": g.uri, "RuleType": g.type, "Scope": g.scope, "UseOvOnly": g.use_ov_only,
            "Condition": sm["Logic"], "NegativeRule": sm["NegativeRule"],
            "Attributes": sm["Attributes"], "ComparatorClasses": sm["ComparatorClasses"],
            "MatchTokenClasses": sm["TokenClasses"], "IgnoreInToken": sm["IgnoreInToken"],
            "Cleansers": sm["Cleansers"], "NullValues": sm["NullValues"], "Weights": sm["Weights"],
            "ActionThresholds": sm["ActionThresholds"],
            "ScoreStandalone": g.score_standalone, "ScoreIncremental": g.score_incremental,
            "PythonFidelity": fid, "TokenFidelity": tok_status, "OtherSettings": sm["OtherSettings"],
        })
    return rows


# ----------------------------------------------------------------------
def _rule_facets(g: MatchGroup) -> Dict[str, str]:
    """Comparable facets of one rule (strings, so the diff is stable and readable)."""
    f = {"type": g.type, "label": g.label, "scope": g.scope, "useOvOnly": str(g.use_ov_only),
         "condition": g.root.describe(),
         "negativeRule": g.negative_root.describe() if g.negative_root else "",
         "actionThresholds": "; ".join(f"{t.type} {t.lo}-{t.hi} ({t.label})" for t in g.thresholds),
         "ignoreInToken": ", ".join(sorted(short_attr(a) for a in g.ignore_in_token)),
         "groupMatchTokenClass": short_class(g.group_token_class),
         "matchServiceClass": short_class(g.match_service_class)}
    for a, m in g.comparators.items():
        f[f"comparator[{short_attr(a)}]"] = m.short + (f" {json.dumps(m.params, sort_keys=True)}" if m.params else "")
    for a, m in g.token_classes.items():
        f[f"matchToken[{short_attr(a)}]"] = m.short + (f" {json.dumps(m.params, sort_keys=True)}" if m.params else "")
    for a, w in g.weights.items():
        f[f"weight[{short_attr(a)}]"] = f"{w:g}"
    for a, vals in g.null_values.items():
        f[f"nullValues[{short_attr(a)}]"] = ", ".join(sorted(vals))
    for c in g.cleansers:
        k = f"cleanse[{short_attr(c.attribute)}]"
        f[k] = ", ".join(sorted(set(filter(None, f.get(k, "").split(", "))) | {short_class(c.adapter)}))
    return f


def compare_configs(source, target, entity_types_: Optional[Sequence[str]] = None,
                    include_unchanged: bool = False) -> List[dict]:
    """Rule-by-rule differences between two configurations (each: any shape load_rule_config accepts,
    or an already parsed list of MatchGroup). Rules are matched by URI."""
    def groups_of(x) -> List[MatchGroup]:
        if isinstance(x, list) and x and isinstance(x[0], MatchGroup):
            return [g for g in x if not entity_types_ or g.entity_type in entity_types_]
        return load_groups(x, entity_types_)

    a = {g.uri: g for g in groups_of(source)}
    b = {g.uri: g for g in groups_of(target)}
    rows = []
    for uri in list(dict.fromkeys(list(a) + list(b))):
        ga, gb = a.get(uri), b.get(uri)
        base = {"EntityType": entity_type_of(uri), "MatchRule": uri.rsplit("/", 1)[-1], "MatchRuleURI": uri}
        if ga is None:
            rows.append({**base, "Change": "ADDED", "Aspect": "rule", "Source": "",
                         "Target": gb.root.describe()})
            continue
        if gb is None:
            rows.append({**base, "Change": "REMOVED", "Aspect": "rule", "Source": ga.root.describe(),
                         "Target": ""})
            continue
        fa, fb = _rule_facets(ga), _rule_facets(gb)
        changed = False
        for k in list(dict.fromkeys(list(fa) + list(fb))):
            va, vb = fa.get(k, ""), fb.get(k, "")
            if va != vb:
                changed = True
                rows.append({**base, "Change": "CHANGED", "Aspect": k, "Source": va, "Target": vb})
        if not changed and include_unchanged:
            rows.append({**base, "Change": "UNCHANGED", "Aspect": "", "Source": "", "Target": ""})
    return rows
