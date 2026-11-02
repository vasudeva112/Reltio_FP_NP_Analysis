"""
Parses match groups out of a Reltio L3 / business configuration export.

Three layers are kept apart, as in Reltio:
  * rule operators   -> Node tree (and / or / not / exact / fuzzy / exactOrNull / ...)
  * comparator classes -> MatchGroup.comparators   (attribute URI -> ClassMapping)
  * match token classes -> MatchGroup.token_classes (attribute URI -> ClassMapping)

Nothing is hardcoded about entity types or attribute names. Unknown operators or
settings are kept on the group and written to the Processing Exceptions log.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from .exceptions_log import LOG

LOGICAL_OPS = {"and", "or", "not"}
ATTRIBUTE_OPS = {"exact", "fuzzy", "exactOrNull", "exactOrAllNull", "exactOrNullOrAllNull", "notExactSame",
                 "notExact", "missing", "notMissing"}
# operators accepted by the parser although they were not found in the Reltio documentation available;
# every use is written to Processing Exceptions as APPROXIMATION
UNDOCUMENTED_OPS = {"exactOrNullOrAllNull", "missing", "notMissing"}
CONSTRAINT_OPS = {"equals", "notEquals", "in", "notIn"}
GROUP_OPS = {"multi"}
KNOWN_OPERATORS = LOGICAL_OPS | ATTRIBUTE_OPS | CONSTRAINT_OPS | GROUP_OPS

RULE_SETTING_KEYS = {"cleanse", "matchTokenClasses", "comparatorClasses", "matchTokenClass",
                     "ignoreInToken", "nullValues", "weights", "actionThresholds"}
GROUP_KEYS = {"uri", "label", "type", "scope", "useOvOnly", "rule", "negativeRule",
              "scoreStandalone", "scoreIncremental", "matchServiceClass"}
RELEVANCE_TYPES = {"relevance_based"}


def short_class(name: str) -> str:
    return (name or "").rsplit(".", 1)[-1]


def entity_type_of(uri: str) -> str:
    m = re.match(r"configuration/entityTypes/([^/]+)", uri or "")
    return m.group(1) if m else ""


def attr_path_parts(attr_uri: str) -> List[str]:
    """configuration/entityTypes/T/attributes/A/attributes/B -> ['A', 'B']"""
    m = re.match(r"configuration/entityTypes/[^/]+/attributes/(.+)$", attr_uri or "")
    if not m:
        return [attr_uri]
    return m.group(1).split("/attributes/")


def short_attr(attr_uri: str) -> str:
    """Dotted path, the same form the extractor uses for column names."""
    return ".".join(attr_path_parts(attr_uri))


def attr_uri_from_dotted(entity_type: str, dotted: str) -> str:
    return f"configuration/entityTypes/{entity_type}/attributes/" + "/attributes/".join(dotted.split("."))


# ----------------------------------------------------------------------
@dataclass
class ClassMapping:
    attribute: str
    class_name: str                      # full Java class name
    params: Dict[str, object] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def short(self) -> str:
        return short_class(self.class_name)


@dataclass
class CleanserMapping:
    adapter: str
    params: Dict[str, object]
    attribute: str
    cleanse_attribute: str
    all_values: bool
    mandatory: bool


@dataclass
class Threshold:
    type: str
    lo: float
    hi: float
    label: str


@dataclass
class Node:
    kind: str                              # 'and' | 'or' | 'not' | 'op'
    path: str
    children: List["Node"] = field(default_factory=list)
    op: str = ""
    attribute: str = ""
    values: List[str] = field(default_factory=list)
    attributes: List[str] = field(default_factory=list)   # multi
    params: Dict[str, object] = field(default_factory=dict)
    supported: bool = True

    def walk(self) -> Iterable["Node"]:
        yield self
        for c in self.children:
            yield from c.walk()

    def describe(self) -> str:
        if self.kind == "op":
            if self.op in CONSTRAINT_OPS:
                return f"{self.op}({short_attr(self.attribute)} in {self.values})"
            if self.op == "multi":
                return f"multi({', '.join(short_attr(a) for a in self.attributes)})"
            return f"{self.op}({short_attr(self.attribute)})"
        inner = ", ".join(c.describe() for c in self.children)
        return f"{self.kind.upper()}[{inner}]"


@dataclass
class MatchGroup:
    uri: str
    label: str
    type: str
    entity_type: str
    scope: str
    use_ov_only: bool
    root: Node
    comparators: Dict[str, ClassMapping]
    token_classes: Dict[str, ClassMapping]
    group_token_class: str
    ignore_in_token: Set[str]
    null_values: Dict[str, Set[str]]
    cleansers: List[CleanserMapping]
    weights: Dict[str, float]
    thresholds: List[Threshold]
    score_standalone: Optional[float]
    score_incremental: Optional[float]
    match_service_class: str
    negative_root: Optional[Node]
    other_settings: Dict[str, object]
    raw: dict

    @property
    def name(self) -> str:
        return self.uri.rsplit("/", 1)[-1]

    @property
    def is_relevance(self) -> bool:
        return self.type in RELEVANCE_TYPES

    def attributes(self) -> List[str]:
        """Every attribute URI the group reads (rule, negative rule, mappings, weights)."""
        seen: List[str] = []

        def add(a: str) -> None:
            if a and a not in seen and "/attributes/" in a:
                seen.append(a)

        for root in (self.root, self.negative_root):
            if root is None:
                continue
            for n in root.walk():
                if n.kind == "op":
                    if n.op == "multi":
                        for a in n.attributes:
                            add(a)
                    else:
                        add(n.attribute)
        return seen

    def weight_for(self, attr_uri: str) -> float:
        if attr_uri in self.weights:
            return self.weights[attr_uri]
        prefix = attr_uri + "/attributes/"
        sub = [w for a, w in self.weights.items() if a.startswith(prefix)]
        if sub:
            return max(sub)
        parts = attr_uri.split("/attributes/")
        for i in range(len(parts) - 1, 1, -1):          # nearest weighted parent (Approximation)
            parent = "/attributes/".join(parts[:i])
            if parent in self.weights:
                return self.weights[parent]
        return 1.0

    def comparator_for(self, attr_uri: str) -> Optional[ClassMapping]:
        return self.comparators.get(attr_uri)

    def summary(self) -> dict:
        ops = Counter(n.op for n in self.root.walk() if n.kind == "op")
        for n in self.root.walk():
            if n.kind in ("or", "not"):
                ops[n.kind] += 1
        return {
            "MatchGroupURI": self.uri,
            "Label": self.label,
            "Type": self.type,
            "EntityType": self.entity_type,
            "Scope": self.scope,
            "UseOvOnly": self.use_ov_only,
            "Operators": ", ".join(f"{k}x{v}" for k, v in sorted(ops.items())),
            "Logic": self.root.describe(),
            "NegativeRule": self.negative_root.describe() if self.negative_root else "",
            "ComparatorClasses": "; ".join(
                f"{short_attr(a)}={m.short}{(' ' + json.dumps(m.params)) if m.params else ''}"
                for a, m in self.comparators.items()),
            "TokenClasses": "; ".join(
                f"{short_attr(a)}={m.short}{(' ' + json.dumps(m.params)) if m.params else ''}"
                for a, m in self.token_classes.items()) + (
                f"; [group]={short_class(self.group_token_class)}" if self.group_token_class else ""),
            "IgnoreInToken": ", ".join(short_attr(a) for a in sorted(self.ignore_in_token)),
            "NullValues": "; ".join(f"{short_attr(a)}={sorted(v)}" for a, v in self.null_values.items()),
            "Cleansers": "; ".join(sorted({f"{short_class(c.adapter)}({short_attr(c.attribute)})"
                                           for c in self.cleansers})),
            "Weights": "; ".join(f"{short_attr(a)}={w}" for a, w in self.weights.items()),
            "ActionThresholds": "; ".join(f"{t.type} {t.lo}-{t.hi} ({t.label})" for t in self.thresholds),
            "Attributes": ", ".join(short_attr(a) for a in self.attributes()),
            "OtherSettings": json.dumps(self.other_settings) if self.other_settings else "",
        }


# ----------------------------------------------------------------------
def _parse_params(mapping: dict) -> Dict[str, object]:
    params: Dict[str, object] = {}
    for p in mapping.get("parameters") or []:
        name = p.get("parameter")
        if "values" in p:
            params[name] = p["values"]
        else:
            params[name] = p.get("value")
    return params


def _parse_mappings(block: Optional[dict]) -> Dict[str, ClassMapping]:
    out: Dict[str, ClassMapping] = {}
    for m in (block or {}).get("mapping", []) or []:
        attr = m.get("attribute", "")
        out[attr] = ClassMapping(attr, m.get("class", ""), _parse_params(m), m)
    return out


def _parse_threshold(t: dict, group: str) -> Optional[Threshold]:
    rng = str(t.get("threshold", ""))
    m = re.match(r"\s*([0-9.]+)\s*-\s*([0-9.]+)\s*$", rng)
    if not m:
        LOG.add("UNPARSED_SETTING", f"actionThreshold range not understood: {rng}", group=group)
        return None
    return Threshold(t.get("type", ""), float(m.group(1)), float(m.group(2)), t.get("label", ""))


class _TreeParser:
    def __init__(self, group_uri: str):
        self.group = group_uri

    def operands(self, obj, path: str) -> List[Node]:
        if isinstance(obj, list):
            nodes = []
            for i, item in enumerate(obj):
                kids = self.operands(item, f"{path}[{i}]")
                nodes.append(kids[0] if len(kids) == 1 else Node("and", f"{path}[{i}]", kids))
            return nodes
        if not isinstance(obj, dict):
            LOG.add("UNSUPPORTED_OPERATOR", f"Unexpected rule element type {type(obj).__name__}",
                    group=self.group, example=f"{path}: {obj!r}")
            return [Node("op", path, op="<invalid>", supported=False)]

        nodes: List[Node] = []
        for key, val in obj.items():
            p = f"{path}/{key}"
            if key in RULE_SETTING_KEYS:
                continue
            if key in ("and", "or"):
                nodes.append(Node(key, p, self.operands(val, p)))
            elif key == "not":
                kids = self.operands(val, p)
                inner = kids[0] if len(kids) == 1 else Node("and", p, kids)
                nodes.append(Node("not", p, [inner]))
            elif key in ATTRIBUTE_OPS:
                if key in UNDOCUMENTED_OPS:
                    LOG.add("APPROXIMATION", f"Operator '{key}' is not in the Reltio documentation available - "
                                             "evaluated with the labelled assumption in OPERATOR_DOCS",
                            group=self.group, example=f"{p}: {json.dumps(val)[:200]}")
                for i, a in enumerate(val if isinstance(val, list) else [val]):
                    nodes.append(Node("op", f"{p}[{i}]", op=key, attribute=a))
            elif key in CONSTRAINT_OPS:
                for i, c in enumerate(val if isinstance(val, list) else [val]):
                    vals = c.get("values")
                    if vals is None:
                        vals = [c.get("value")]
                    params = {k: v for k, v in c.items() if k not in ("uri", "value", "values")}
                    nodes.append(Node("op", f"{p}[{i}]", op=key, attribute=c.get("uri", ""),
                                      values=[str(v) for v in vals if v is not None], params=params))
            elif key == "multi":
                for i, c in enumerate(val if isinstance(val, list) else [val]):
                    nodes.append(Node("op", f"{p}[{i}]", op="multi", attribute=c.get("uri", ""),
                                      attributes=list(c.get("attributes") or [])))
            else:
                LOG.add("UNSUPPORTED_OPERATOR", f"Operator '{key}' is not implemented",
                        group=self.group, example=f"{p}: {json.dumps(val)[:200]}", severity="ERROR")
                nodes.append(Node("op", p, op=key, supported=False, params={"raw": val}))
        return nodes


def _parse_rule_block(rule: dict, group_uri: str, path: str) -> Node:
    kids = _TreeParser(group_uri).operands(rule, path)
    return Node("and", path, kids)


def parse_match_group(g: dict) -> MatchGroup:
    uri = g.get("uri", "")
    rule = g.get("rule") or {}
    other = {k: v for k, v in g.items() if k not in GROUP_KEYS}
    for k in other:
        LOG.add("UNPARSED_SETTING", f"Match group setting '{k}' is not interpreted", group=uri,
                example=json.dumps(other[k])[:200])

    null_values: Dict[str, Set[str]] = {}
    for nv in rule.get("nullValues") or []:
        null_values.setdefault(nv.get("attribute", ""), set()).add(str(nv.get("value", "")))

    cleansers: List[CleanserMapping] = []
    for c in rule.get("cleanse") or []:
        for m in c.get("mappings") or []:
            cleansers.append(CleanserMapping(
                adapter=c.get("cleanseAdapter", ""),
                params=dict(c.get("cleanseAdapterParams") or {}),
                attribute=m.get("attribute", ""),
                cleanse_attribute=m.get("cleanseAttribute", m.get("attribute", "")),
                all_values=bool(m.get("allValues", False)),
                mandatory=bool(m.get("mandatory", False))))

    weights = {w["attribute"]: float(w.get("weight", 1.0)) for w in rule.get("weights") or []
               if "attribute" in w}
    thresholds = [t for t in (_parse_threshold(t, uri) for t in rule.get("actionThresholds") or []) if t]

    neg = g.get("negativeRule")
    negative_root = _parse_rule_block(neg, uri, "negativeRule") if isinstance(neg, dict) else None

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    mg = MatchGroup(
        uri=uri,
        label=g.get("label") or uri.rsplit("/", 1)[-1],
        type=g.get("type", ""),
        entity_type=entity_type_of(uri),
        scope=str(g.get("scope", "")),
        use_ov_only=str(g.get("useOvOnly", "true")).lower() != "false",
        root=_parse_rule_block(rule, uri, "rule"),
        comparators=_parse_mappings(rule.get("comparatorClasses")),
        token_classes=_parse_mappings(rule.get("matchTokenClasses")),
        group_token_class=rule.get("matchTokenClass", "") or "",
        ignore_in_token=set(rule.get("ignoreInToken") or []),
        null_values=null_values,
        cleansers=cleansers,
        weights=weights,
        thresholds=thresholds,
        score_standalone=num(g.get("scoreStandalone")),
        score_incremental=num(g.get("scoreIncremental")),
        match_service_class=g.get("matchServiceClass", "") or "",
        negative_root=negative_root,
        other_settings=other,
        raw=g,
    )
    if mg.is_relevance and not mg.thresholds:
        LOG.add("CONFIG_FINDING", "relevance_based group has no actionThresholds - no pair can match",
                group=uri, severity="ERROR")
    if mg.type not in ("suspect", "automatic", "automatic_grouping", "relevance_based"):
        LOG.add("UNPARSED_SETTING", f"Match group type '{mg.type}' evaluated as boolean (suspect-like)",
                group=uri)
    _config_findings(mg)
    return mg


def _config_findings(mg: MatchGroup) -> None:
    """Reports mapping/rule inconsistencies; does not change anything."""
    rule_attrs = set(mg.attributes())
    for n in mg.root.walk():
        if n.kind == "op" and n.op == "multi":
            rule_attrs.add(n.attribute)
    for a in rule_attrs:
        needs_cmp = any(n.kind == "op" and n.attribute == a and n.op in ATTRIBUTE_OPS
                        for n in mg.root.walk())
        if needs_cmp and a not in mg.comparators:
            LOG.add("CONFIG_FINDING",
                    "Attribute compared by the rule has no comparatorClasses mapping - "
                    "default BasicStringComparator assumed",
                    group=mg.uri, attribute=short_attr(a), severity="INFO")
    for a in mg.comparators:
        if a not in rule_attrs and not any(a == x for n in mg.root.walk() for x in n.attributes):
            LOG.add("CONFIG_FINDING",
                    "comparatorClasses maps an attribute the rule does not use (possible ATTRIBUTE_PATH_MISMATCH)",
                    group=mg.uri, attribute=short_attr(a), severity="INFO")
    for a in mg.weights:
        if a not in rule_attrs:
            LOG.add("CONFIG_FINDING", "weights reference an attribute the rule does not use",
                    group=mg.uri, attribute=short_attr(a), severity="INFO")
    for c in mg.cleansers:
        if c.attribute != c.cleanse_attribute:
            LOG.add("CONFIG_FINDING", "Cleanser writes to a different attribute than it reads",
                    group=mg.uri, attribute=short_attr(c.attribute), severity="INFO")


# ----------------------------------------------------------------------
def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def parse_match_groups(config: dict, entity_types: Optional[List[str]] = None,
                       target_rule: str = "") -> List[MatchGroup]:
    """
    entity_types: names such as ['Individual']; empty = every entity type.
    target_rule: same syntax as the extractor's TARGET_RULE (comma list of names or URIs,
    blank / AllRules = all).
    """
    wanted_types = {t.strip() for t in entity_types or [] if t.strip()}
    groups: List[MatchGroup] = []
    for et in config.get("entityTypes", []) or []:
        name = entity_type_of(et.get("uri", ""))
        if wanted_types and name not in wanted_types:
            continue
        for g in et.get("matchGroups") or []:
            groups.append(parse_match_group(g))

    tr = (target_rule or "").strip()
    if tr and tr.lower() != "allrules":
        wanted = []
        for t in (x.strip() for x in tr.split(",") if x.strip()):
            wanted.append(t.lower() if "/" in t else t.lower())
        sel = [g for g in groups if g.uri.lower() in wanted or g.name.lower() in wanted]
        missing = [w for w in wanted if not any(w in (g.uri.lower(), g.name.lower()) for g in groups)]
        for m in missing:
            LOG.add("INPUT_ERROR", f"TARGET_RULE entry not found in configuration: {m}", severity="ERROR")
        groups = sel
    return groups


def inventory(groups: List[MatchGroup]) -> Dict[str, Counter]:
    inv = {k: Counter() for k in ("types", "operators", "comparators", "token_classes",
                                  "cleansers", "settings", "nested_comparators")}
    for g in groups:
        inv["types"][g.type] += 1
        for n in g.root.walk():
            inv["operators"][n.op if n.kind == "op" else n.kind] += 1
        for m in g.comparators.values():
            inv["comparators"][m.short] += 1
            for grp in m.params.get("groups") or []:
                inv["nested_comparators"][short_class(grp.get("className", ""))] += 1
        for m in g.token_classes.values():
            inv["token_classes"][m.short] += 1
            for grp in m.params.get("groups") or []:
                inv["nested_comparators"][short_class(grp.get("className", ""))] += 1
        if g.group_token_class:
            inv["token_classes"][short_class(g.group_token_class)] += 1
        for c in g.cleansers:
            inv["cleansers"][short_class(c.adapter)] += 1
        for k in g.raw:
            inv["settings"][k] += 1
        for k in (g.raw.get("rule") or {}):
            if k in RULE_SETTING_KEYS:
                inv["settings"][f"rule.{k}"] += 1
    return inv
