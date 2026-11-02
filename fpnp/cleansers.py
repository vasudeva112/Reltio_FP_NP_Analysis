"""
Match-time cleansers (rule.cleanse) and nullValues.

Order applied per value (Approximation - Reltio does not document the relative order
of nullValues and cleansers): nullValues on the raw value, then cleansers.
Cleansed values are used only for comparison and tokens, never written anywhere.
"""

import csv
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .attribute_extractor import NULL_VALUE, AttrValues, Leaf
from .comparators.base import APPROXIMATION, EngineContext
from .exceptions_log import LOG
from .rule_config_parser import MatchGroup, short_attr, short_class


@dataclass
class PreparedValues:
    """Attribute values of one entity after nullValues + cleansers, for one group."""
    state: str
    leaves: Tuple[Leaf, ...]            # cleansed values, original instance ids
    raw: Tuple[str, ...]                 # original values (same order as leaves)
    nulled: Tuple[str, ...] = ()         # values removed by nullValues
    cleanse_notes: Tuple[str, ...] = ()  # e.g. "NameDictionaryCleanser:identity"

    @property
    def has_value(self) -> bool:
        return bool(self.leaves)

    @property
    def values(self) -> List[str]:
        return [l.value for l in self.leaves]

    def display(self) -> str:
        if not self.leaves:
            return f"<{self.state}>" + (f" nulled={list(self.nulled)}" if self.nulled else "")
        shown = []
        for raw, leaf in zip(self.raw, self.leaves):
            shown.append(raw if raw == leaf.value else f"{raw}=>{leaf.value}")
        return " | ".join(dict.fromkeys(shown))


# ----------------------------------------------------------------------
def load_name_dictionary(path: str) -> Dict[str, str]:
    """CSV: variant,canonical (header optional). Lower-cased keys."""
    out: Dict[str, str] = {}
    if not path:
        return out
    with open(path, encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[0].strip() and not row[0].lower().startswith("variant"):
                out[row[0].strip().lower()] = row[1].strip()
    return out


def load_regexp_rules(path: str) -> List[Tuple[re.Pattern, str]]:
    """One rule per line: <regex><TAB><replacement> (or comma-separated)."""
    rules = []
    if not path:
        return rules
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            sep = "\t" if "\t" in line else ","
            pat, _, repl = line.partition(sep)
            rules.append((re.compile(pat, re.I), repl))
    return rules


def load_noise_dictionary(path: str) -> set:
    with open(path, encoding="utf-8") as fh:
        return {w.strip().lower() for w in fh if w.strip()}


# ----------------------------------------------------------------------
class GroupCleansers:
    STATUS = APPROXIMATION

    def __init__(self, group: MatchGroup, ctx: EngineContext):
        self.ctx = ctx
        self.group = group
        self.by_attr: Dict[str, List[str]] = {}
        for c in group.cleansers:
            self.by_attr.setdefault(c.attribute, []).append(c.adapter)
            name = short_class(c.adapter)
            if name == "NameDictionaryCleanser" and not ctx.name_dictionary:
                LOG.add("APPROXIMATION", "NameDictionaryCleanser: Reltio name dictionary not available - "
                                         "identity transform (a real dictionary can only make more values equal)",
                        group=group.uri, attribute=short_attr(c.attribute), severity="INFO")
            elif name == "RegexpReplaceCleanser" and not ctx.regexp_rules:
                LOG.add("APPROXIMATION", "RegexpReplaceCleanser: dictionary "
                                         f"{c.params.get('dictionary', '')} not available locally - identity",
                        group=group.uri, attribute=short_attr(c.attribute))
            elif name not in ("NameDictionaryCleanser", "RegexpReplaceCleanser"):
                LOG.add("UNSUPPORTED_CLEANSER", f"Cleanser {name} not implemented - identity",
                        group=group.uri, attribute=short_attr(c.attribute), severity="ERROR")
            if c.attribute != c.cleanse_attribute:
                LOG.add("UNSUPPORTED_PARAMETER", "cleanseAttribute differs from attribute - "
                                                 "cleansed value applied to the source attribute",
                        group=group.uri, attribute=short_attr(c.attribute))
        self.null_values = {a: {ctx.norm(v) for v in vals} for a, vals in group.null_values.items()}

    def _cleanse(self, adapter: str, value: str) -> Tuple[str, str]:
        name = short_class(adapter)
        if name == "NameDictionaryCleanser":
            if self.ctx.name_dictionary:
                new = self.ctx.name_dictionary.get(value.strip().lower())
                return (new, f"{name}:mapped") if new else (value, f"{name}:no-entry")
            return value, f"{name}:identity"
        if name == "RegexpReplaceCleanser":
            if not self.ctx.regexp_rules:
                return value, f"{name}:identity"
            new = value
            for pat, repl in self.ctx.regexp_rules:
                new = pat.sub(repl, new)
            return new.strip(), f"{name}:applied"
        return value, f"{name}:unsupported"

    def prepare(self, attr_uri: str, av: AttrValues) -> PreparedValues:
        nulls = self.null_values.get(attr_uri)
        adapters = self.by_attr.get(attr_uri, [])
        leaves, raw, nulled, notes = [], [], [], []
        for leaf in av.leaves:
            v = leaf.value
            if nulls and self.ctx.norm(v) in nulls:
                nulled.append(v)
                continue
            cv = v
            for ad in adapters:
                cv, note = self._cleanse(ad, cv)
                notes.append(note)
            if not cv.strip():
                nulled.append(v)
                continue
            leaves.append(Leaf(cv, leaf.instance))
            raw.append(v)
        state = av.state if leaves or not nulled else NULL_VALUE
        return PreparedValues(state, tuple(leaves), tuple(raw), tuple(nulled),
                              tuple(dict.fromkeys(notes)))
