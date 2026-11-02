"""
Step 1 of FN analysis - load Reltio entities.json exports.

Streams any number of exports (JSON array, JSON Lines, {"objects": [...]} pages, .gz, directories -
via the existing json_loader, which never reads a whole array into memory) and keeps only:

  * the attributes the selected match rules use, as OV values (ov=true) - non-OV source values are
    not used as golden values; nested and multi-value attributes keep their instance ids and URIs
  * per entity: URI, entity type, crosswalk keys (source:value, ReltioCleanser excluded), number of
    crosswalks and sources, created / updated metadata, source file

It also detects records that are already the same golden record:
  * the same URI exported twice                         (first kept)
  * two URIs sharing a crosswalk (same source record)   -> pair marked "already merged / same source record"
  * an optional merge map CSV (loser,winner)            -> pairs resolving to the same winner are merged
"""

import csv
import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..attribute_extractor import build_record
from ..candidate_generation import EntityStore
from ..exceptions_log import LOG
from ..json_loader import iter_entities
from ..legacy import EXCLUDED_CROSSWALK_SOURCES
from ..pair_deduplication import normalise_uri, pair_key
from ..rule_config_parser import MatchGroup, attr_path_parts

log = logging.getLogger("fpnp")


@dataclass
class EntityMeta:
    uri: str
    entity_type: str
    crosswalk_keys: Tuple[str, ...]
    crosswalk_count: int
    sources: Tuple[str, ...]
    created: str
    updated: str
    source_file: str


@dataclass
class LoadedEntities:
    store: EntityStore
    meta: List[EntityMeta] = field(default_factory=list)           # aligned with store.records
    merged_pairs: Dict[str, str] = field(default_factory=dict)       # pair key -> reason
    stats: Dict[str, object] = field(default_factory=dict)

    def meta_for(self, i: int) -> EntityMeta:
        return self.meta[i]


def _crosswalk_info(cws) -> Tuple[Tuple[str, ...], int, Tuple[str, ...]]:
    keys, sources = [], []
    for cw in cws or []:
        if not isinstance(cw, dict):
            continue
        src = (cw.get("type") or "").replace("configuration/sources/", "")
        if src.lower() in EXCLUDED_CROSSWALK_SOURCES:
            continue
        sources.append(src)
        if cw.get("value") not in (None, ""):
            keys.append(f"{src}:{cw.get('value')}" + (f"@{cw['sourceTable']}" if cw.get("sourceTable") else ""))
    return tuple(dict.fromkeys(keys)), len(keys), tuple(dict.fromkeys(sources))


def _ts(v) -> str:
    if v in (None, ""):
        return ""
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(int(v) / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(v)


def load_merge_map(path: str) -> Dict[str, str]:
    """CSV with columns loser,winner (header optional) -> {loser uri: winner uri}."""
    out: Dict[str, str] = {}
    if not path:
        return out
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[0].strip() and row[0].strip().lower() not in ("loser", "loseruri"):
                out[normalise_uri(row[0])] = normalise_uri(row[1])
    return out


def load_known_pairs(path: str) -> Dict[str, str]:
    """CSV of pairs already matched / linked / flagged (EntityURI1,EntityURI2[,Reason]) -> {pair key: reason}."""
    out: Dict[str, str] = {}
    if not path:
        return out
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        for row in rd:
            if len(row) < 2 or not row[0].strip() or row[0].strip().lower().startswith(("entity", "src", "uri")):
                continue
            out[pair_key(row[0], row[1])] = (row[2].strip() if len(row) > 2 and row[2].strip()
                                             else "listed in known matches file")
    return out


def load_entities(paths: Sequence[str], groups: Sequence[MatchGroup], entity_types: Optional[Sequence[str]] = None,
                  treat_blank_as_null: bool = True, merge_map: Optional[Dict[str, str]] = None,
                  progress: Optional[Callable[[int], None]] = None, progress_every: int = 50_000) -> LoadedEntities:
    store = EntityStore()
    out = LoadedEntities(store)
    wanted = set(entity_types or []) or {g.entity_type for g in groups}
    attrs_by_type: Dict[str, List[str]] = defaultdict(list)
    non_ov_types = set()
    for g in groups:
        if g.entity_type not in wanted:
            continue
        for a in g.attributes():
            if a not in attrs_by_type[g.entity_type]:
                attrs_by_type[g.entity_type].append(a)
        if not g.use_ov_only:
            non_ov_types.add(g.entity_type)
    top_keys = {et: {attr_path_parts(a)[0] for a in attrs} for et, attrs in attrs_by_type.items()}

    cw_owner: Dict[str, int] = {}
    types = Counter()
    other_types = Counter()
    non_ov_values = 0
    read = 0
    for src, ent in iter_entities(list(paths)):
        read += 1
        et = (ent.get("type") or "").rsplit("/", 1)[-1]
        if et not in attrs_by_type:
            other_types[et] += 1
            continue
        if not ent.get("uri"):
            LOG.add("INVALID_ENTITY", "Entity without uri skipped", source=src)
            continue
        before = len(store)
        rec = build_record(ent, attrs_by_type[et], et in non_ov_types, treat_blank_as_null, src)
        store.add(rec)
        if len(store) == before:                       # duplicate URI - first kept
            continue
        types[et] += 1
        attrs = ent.get("attributes") or {}
        for k in top_keys[et]:
            for v in attrs.get(k) or []:
                if isinstance(v, dict) and v.get("ov") is False:
                    non_ov_values += 1
        keys, n_cw, sources = _crosswalk_info(ent.get("crosswalks"))
        idx = len(store) - 1
        out.meta.append(EntityMeta(rec.uri, et, keys, n_cw, sources, _ts(ent.get("createdTime")),
                                   _ts(ent.get("updatedTime")), os.path.basename(src)))
        for k in keys:
            other = cw_owner.get(k)
            if other is None:
                cw_owner[k] = idx
            elif store.records[other].uri != rec.uri:
                out.merged_pairs.setdefault(pair_key(store.records[other].uri, rec.uri),
                                            f"same source record (shared crosswalk {k})")
        if progress and read % progress_every == 0:
            progress(read)
    if merge_map:
        by_winner: Dict[str, List[str]] = defaultdict(list)
        present = set(store.index)
        for loser, winner in merge_map.items():
            if loser in present:
                by_winner[winner].append(loser)
        for winner, losers in by_winner.items():
            members = losers + ([winner] if winner in present else [])
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    out.merged_pairs.setdefault(pair_key(members[i], members[j]), f"merged into {winner} (merge map)")
    out.stats = {"entities_read": read, "entities_loaded": len(store), "entity_types": dict(types),
                 "skipped_other_types": dict(other_types), "duplicate_uris": store.stats["duplicates"],
                 "non_ov_values_ignored": non_ov_values, "merged_or_same_record_pairs": len(out.merged_pairs),
                 "attributes_by_type": {k: len(v) for k, v in attrs_by_type.items()}}
    log.info("Entities loaded: %s", out.stats)
    return out
