"""
Resolves configured attribute URIs against entity JSON.

Values are found by attribute URI path (Address/attributes/PostalCode/attributes/Zip5),
never by position or display name. The state of an attribute is kept explicit:

  PRESENT       at least one usable value
  MISSING       attribute key absent
  NULL          explicit JSON null (attribute or value)
  EMPTY_STRING  ""                         (treated as no value when treat_blank_as_null)
  WHITESPACE    "   "                      (idem)
  EMPTY_LIST    []
  NO_OV_VALUE   values exist but none is ov=true while the group uses OV only
  NULL_VALUE    value replaced by the group's nullValues list
  UNKNOWN_FROM_REPORT  report column empty - missing/null/empty cannot be told apart
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .legacy import VALUE_SEP, format_crosswalks
from .rule_config_parser import attr_path_parts, attr_uri_from_dotted

PRESENT = "PRESENT"
MISSING = "MISSING"
NULL = "NULL"
EMPTY_STRING = "EMPTY_STRING"
WHITESPACE = "WHITESPACE"
EMPTY_LIST = "EMPTY_LIST"
NO_OV_VALUE = "NO_OV_VALUE"
NULL_VALUE = "NULL_VALUE"
UNKNOWN_FROM_REPORT = "UNKNOWN_FROM_REPORT"

_STATE_PRIORITY = [NULL_VALUE, NO_OV_VALUE, NULL, EMPTY_LIST, EMPTY_STRING, WHITESPACE,
                   UNKNOWN_FROM_REPORT, MISSING]


@dataclass(frozen=True)
class Leaf:
    value: str
    instance: str            # nested instance id, e.g. "Identifiers[1]" or the value URI


@dataclass
class AttrValues:
    state: str
    leaves: Tuple[Leaf, ...] = ()
    empty_states: Tuple[str, ...] = ()     # states of individual null/empty values in a multi-value list

    @property
    def has_value(self) -> bool:
        return bool(self.leaves)

    @property
    def values(self) -> List[str]:
        return [l.value for l in self.leaves]

    def display(self) -> str:
        if self.leaves:
            return VALUE_SEP.join(dict.fromkeys(self.values))
        return f"<{self.state}>"


def _combine_state(states: Iterable[str]) -> str:
    s = set(states)
    for st in _STATE_PRIORITY:
        if st in s:
            return st
    return MISSING


def _leaf_state(v) -> Tuple[Optional[str], str]:
    if v is None:
        return None, NULL
    if isinstance(v, str):
        if v == "":
            return None, EMPTY_STRING
        if not v.strip():
            return None, WHITESPACE
        return v, PRESENT
    if isinstance(v, (list, dict)) and not v:
        return None, EMPTY_LIST
    return str(v), PRESENT


def _render_nested(value: dict) -> str:
    parts = []
    for sub, sub_vals in sorted(value.items()):
        if isinstance(sub_vals, list):
            vals = [str(x.get("value")) for x in sub_vals
                    if isinstance(x, dict) and not isinstance(x.get("value"), dict) and x.get("value") is not None]
            if vals:
                parts.append(f"{sub}={','.join(vals)}")
    return "; ".join(parts)


def extract(attributes: dict, parts: List[str], ov_only: bool = True,
            treat_blank_as_null: bool = True, _top: str = "") -> AttrValues:
    """Leaf.instance is the id of the top-level value the leaf belongs to, so sub-attributes
    of the same nested instance (e.g. Identifiers Type and Value) share it."""
    if not isinstance(attributes, dict) or parts[0] not in attributes:
        return AttrValues(MISSING)
    raw = attributes[parts[0]]
    if raw is None:
        return AttrValues(NULL)
    if not isinstance(raw, list):
        raw = [raw]
    if not raw:
        return AttrValues(EMPTY_LIST)

    leaves: List[Leaf] = []
    empties: List[str] = []
    saw_non_ov = False
    for idx, v in enumerate(raw):
        if not isinstance(v, dict):
            v = {"value": v}
        if ov_only and v.get("ov") is False:
            saw_non_ov = True
            continue
        inst = _top or v.get("uri") or f"{parts[0]}[{idx}]"
        inner = v.get("value")
        if len(parts) == 1:
            if isinstance(inner, dict) and inner:
                rendered = _render_nested(inner)
                if rendered:
                    leaves.append(Leaf(rendered, inst))
                else:
                    empties.append(EMPTY_LIST)
                continue
            val, st = _leaf_state(inner)
            if val is not None:
                leaves.append(Leaf(val, inst))
            elif st in (EMPTY_STRING, WHITESPACE) and not treat_blank_as_null:
                leaves.append(Leaf(inner, inst))
            else:
                empties.append(st)
        else:
            if isinstance(inner, dict):
                sub = extract(inner, parts[1:], ov_only, treat_blank_as_null, inst)
                leaves.extend(sub.leaves)
                if not sub.leaves:
                    empties.append(sub.state)
                empties.extend(sub.empty_states)
            else:
                empties.append(NULL if inner is None else MISSING)

    if leaves:
        return AttrValues(PRESENT, tuple(leaves), tuple(empties))
    if saw_non_ov and not empties:
        return AttrValues(NO_OV_VALUE)
    return AttrValues(_combine_state(empties) if empties else MISSING, (), tuple(empties))


# ----------------------------------------------------------------------
@dataclass
class EntityRecord:
    uri: str
    entity_type: str
    crosswalks: str
    values_ov: Dict[str, AttrValues] = field(default_factory=dict)
    values_all: Dict[str, AttrValues] = field(default_factory=dict)
    origin: str = "entity_export"          # or "report_values"
    source: str = ""

    def get(self, attr_uri: str, ov_only: bool = True) -> AttrValues:
        store = self.values_ov if ov_only or not self.values_all else self.values_all
        return store.get(attr_uri, AttrValues(MISSING))


def build_record(entity: dict, attr_uris: Iterable[str], need_non_ov: bool = False,
                 treat_blank_as_null: bool = True, source: str = "") -> EntityRecord:
    attrs = entity.get("attributes") or {}
    rec = EntityRecord(
        uri=entity.get("uri", ""),
        entity_type=(entity.get("type") or "").rsplit("/", 1)[-1],
        crosswalks=format_crosswalks(entity.get("crosswalks")),
        source=source,
    )
    for a in attr_uris:
        parts = attr_path_parts(a)
        rec.values_ov[a] = extract(attrs, parts, True, treat_blank_as_null)
        if need_non_ov:
            rec.values_all[a] = extract(attrs, parts, False, treat_blank_as_null)
    return rec


def record_from_report(uri: str, entity_type: str, crosswalks: str,
                       dotted_values: Dict[str, str]) -> EntityRecord:
    """Pseudo-entity built from Potential Match Report columns (OV values only)."""
    rec = EntityRecord(uri=uri, entity_type=entity_type, crosswalks=crosswalks, origin="report_values")
    for dotted, text in dotted_values.items():
        a = attr_uri_from_dotted(entity_type, dotted)
        vals = [v.strip() for v in (text or "").split(VALUE_SEP) if v.strip()]
        if vals:
            # the report does not say which nested instance a value came from
            rec.values_ov[a] = AttrValues(PRESENT, tuple(Leaf(v, "") for v in vals))
        else:
            rec.values_ov[a] = AttrValues(UNKNOWN_FROM_REPORT)
    return rec
