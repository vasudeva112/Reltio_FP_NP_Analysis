"""
Entity store and candidate-pair generation.

Sources are kept distinct:
  TOKEN        pair shares a non-over-collided token phrase of a match group
               (Reltio-equivalent candidate selection, approximated)
  OVERCOLLIDED pair shares a phrase held by more than MAX_TOKEN_COLLISION profiles
               (Reltio skips these phrases)
  BLOCKING     recall-oriented extra blocking (exact single attribute, attribute pairs,
               phonetic+exact pairs, sorted neighbourhood)
  EXHAUSTIVE   every pair (small datasets / recall measurement only)
"""

import itertools
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .attribute_extractor import EntityRecord, build_record
from .comparators.phonetic import double_metaphone
from .exceptions_log import LOG
from .json_loader import iter_entities
from .match_tokens import GroupTokenizer
from .pair_deduplication import normalise_uri
from .rule_config_parser import MatchGroup, short_attr
from .rule_evaluator import RuleEngine

log = logging.getLogger("fpnp")

BLOCK_SINGLE = 1
BLOCK_PAIR = 2
BLOCK_PHONETIC = 4
BLOCK_SORTED = 8
BLOCK_NAMES = {BLOCK_SINGLE: "exact-single", BLOCK_PAIR: "exact-pair", BLOCK_PHONETIC: "phonetic-pair",
               BLOCK_SORTED: "sorted-neighbourhood"}
MAX_VALUES_PER_ATTR = 5


# ----------------------------------------------------------------------
class EntityStore:
    def __init__(self):
        self.records: List[EntityRecord] = []
        self.index: Dict[str, int] = {}
        self.by_type: Dict[str, List[int]] = defaultdict(list)
        self.stats = {"entities_read": 0, "entities_loaded": 0, "skipped_other_type": 0,
                      "duplicates": 0, "missing_uri": 0}

    def add(self, rec: EntityRecord) -> None:
        rec.uri = normalise_uri(rec.uri)
        if rec.uri in self.index:
            self.stats["duplicates"] += 1
            LOG.add("DUPLICATE_ENTITY", "Entity appears more than once in the exports - first kept",
                    example=rec.uri)
            return
        self.index[rec.uri] = len(self.records)
        self.by_type[rec.entity_type].append(len(self.records))
        self.records.append(rec)
        self.stats["entities_loaded"] += 1

    def get(self, uri: str) -> Optional[EntityRecord]:
        i = self.index.get(normalise_uri(uri))
        return None if i is None else self.records[i]

    def __len__(self) -> int:
        return len(self.records)

    @classmethod
    def load(cls, paths: List[str], groups: List[MatchGroup], treat_blank_as_null: bool = True,
             progress_every: int = 100_000) -> "EntityStore":
        store = cls()
        attrs_by_type: Dict[str, List[str]] = defaultdict(list)
        non_ov_types = set()
        for g in groups:
            for a in g.attributes():
                if a not in attrs_by_type[g.entity_type]:
                    attrs_by_type[g.entity_type].append(a)
            if not g.use_ov_only:
                non_ov_types.add(g.entity_type)
        for src, ent in iter_entities(paths):
            store.stats["entities_read"] += 1
            et = (ent.get("type") or "").rsplit("/", 1)[-1]
            if et not in attrs_by_type:
                store.stats["skipped_other_type"] += 1
                continue
            if not ent.get("uri"):
                store.stats["missing_uri"] += 1
                LOG.add("INVALID_ENTITY", "Entity without uri skipped", source=src)
                continue
            store.add(build_record(ent, attrs_by_type[et], et in non_ov_types, treat_blank_as_null, src))
            if store.stats["entities_read"] % progress_every == 0:
                log.info("Loaded %d entities (%d kept)", store.stats["entities_read"], len(store))
        return store


# ----------------------------------------------------------------------
@dataclass
class PairSource:
    token_groups: Set[str] = field(default_factory=set)
    overcollided_groups: Set[str] = field(default_factory=set)
    blocking: int = 0
    exhaustive: bool = False

    def label(self, group_uri: Optional[str] = None) -> str:
        if group_uri and group_uri in self.token_groups:
            return "TOKEN_SAME_GROUP"
        if self.token_groups:
            return "TOKEN_OTHER_GROUP"
        if self.overcollided_groups:
            return "OVERCOLLIDED_TOKEN_ONLY"
        if self.blocking:
            return "BLOCKING_ONLY"
        return "EXHAUSTIVE_ONLY" if self.exhaustive else "UNKNOWN"

    def blocking_names(self) -> str:
        return ",".join(n for b, n in BLOCK_NAMES.items() if self.blocking & b)

    def as_dict(self) -> dict:
        return {"token_groups": sorted(self.token_groups), "overcollided_groups": sorted(self.overcollided_groups),
                "blocking": self.blocking, "exhaustive": self.exhaustive}

    @classmethod
    def from_dict(cls, d: dict) -> "PairSource":
        return cls(set(d.get("token_groups", [])), set(d.get("overcollided_groups", [])),
                   int(d.get("blocking", 0)), bool(d.get("exhaustive", False)))


@dataclass
class CandidateStats:
    by_group: Dict[str, dict] = field(default_factory=dict)
    blocks_skipped: int = 0
    blocks_used: int = 0
    seconds: float = 0.0

    def group(self, uri: str) -> dict:
        return self.by_group.setdefault(uri, {"entities_with_phrases": 0, "phrases": 0, "shared_phrases": 0,
                                              "overcollided_phrases": 0, "token_pairs": 0,
                                              "overcollided_pairs": 0, "overcollided_pairs_not_enumerated": 0})


class CandidateGenerator:
    def __init__(self, store: EntityStore, groups: List[MatchGroup], engine: RuleEngine, settings):
        self.store = store
        self.groups = groups
        self.engine = engine
        self.s = settings
        self.pairs: Dict[Tuple[int, int], PairSource] = {}
        self.stats = CandidateStats()

    def _src(self, i: int, j: int) -> PairSource:
        key = (i, j) if i < j else (j, i)
        ps = self.pairs.get(key)
        if ps is None:
            ps = self.pairs[key] = PairSource()
        return ps

    def _bucket_pairs(self, members: List[int]) -> Iterable[Tuple[int, int]]:
        return itertools.combinations(sorted(set(members)), 2)

    # ---- token logic -------------------------------------------------------
    def token_pass(self, etype: str, idxs: List[int]) -> None:
        for g in (x for x in self.groups if x.entity_type == etype):
            st = self.stats.group(g.uri)
            tok = GroupTokenizer(g, self.engine.ctx, self.s.max_phrases_per_entity)
            buckets: Dict[str, List[int]] = defaultdict(list)
            for i in idxs:
                phrases = tok.phrases(self.engine.prepare(g, self.store.records[i]))
                if phrases:
                    st["entities_with_phrases"] += 1
                for p in phrases:
                    buckets[p].append(i)
            st["phrases"] = len(buckets)
            for phrase, members in buckets.items():
                if len(members) < 2:
                    continue
                st["shared_phrases"] += 1
                if len(members) > self.s.max_token_collision:
                    st["overcollided_phrases"] += 1
                    if len(members) > self.s.blocking_max_block_size:
                        st["overcollided_pairs_not_enumerated"] += len(members) * (len(members) - 1) // 2
                        LOG.add("OVERCOLLIDED_TOKEN", f"Phrase shared by {len(members)} entities: pairs not "
                                                      "enumerated (coverage limitation)", group=g.uri,
                                example=phrase[:200])
                        continue
                    for i, j in self._bucket_pairs(members):
                        self._src(i, j).overcollided_groups.add(g.uri)
                        st["overcollided_pairs"] += 1
                    continue
                for i, j in self._bucket_pairs(members):
                    self._src(i, j).token_groups.add(g.uri)
                    st["token_pairs"] += 1

    # ---- recall blocking ---------------------------------------------------
    def _norm_values(self, rec: EntityRecord, attr: str) -> List[str]:
        vals = [self.engine.ctx.norm(v) for v in rec.get(attr).values]
        return sorted(set(v for v in vals if v))[:MAX_VALUES_PER_ATTR]

    def blocking_pass(self, etype: str, idxs: List[int]) -> None:
        gs = [g for g in self.groups if g.entity_type == etype]
        attrs: List[str] = []
        pairs: Set[Tuple[str, str]] = set()
        for g in gs:
            ga = g.attributes()
            attrs.extend(a for a in ga if a not in attrs)
            pairs.update(tuple(sorted(p)) for p in itertools.combinations(ga, 2))
        buckets: Dict[Tuple[int, str], List[int]] = defaultdict(list)
        for i in idxs:
            rec = self.store.records[i]
            vals = {a: self._norm_values(rec, a) for a in attrs}
            for a in attrs:
                for v in vals[a]:
                    buckets[(BLOCK_SINGLE, f"{a}={v}")].append(i)
            if self.s.blocking_attribute_pairs:
                for a, b in pairs:
                    for va in vals[a]:
                        for vb in vals[b]:
                            buckets[(BLOCK_PAIR, f"{a}={va}&{b}={vb}")].append(i)
                            if self.s.blocking_phonetic:
                                for code in double_metaphone(va)[:1]:
                                    buckets[(BLOCK_PHONETIC, f"{a}~{code}&{b}={vb}")].append(i)
                                for code in double_metaphone(vb)[:1]:
                                    buckets[(BLOCK_PHONETIC, f"{a}={va}&{b}~{code}")].append(i)
        for (kind, key), members in buckets.items():
            if len(members) < 2:
                continue
            if len(members) > self.s.blocking_max_block_size:
                self.stats.blocks_skipped += 1
                LOG.add("BLOCK_TOO_LARGE", f"{BLOCK_NAMES[kind]} block over BLOCKING_MAX_BLOCK_SIZE skipped",
                        example=f"{key[:150]} ({len(members)} entities)", severity="INFO")
                continue
            self.stats.blocks_used += 1
            for i, j in self._bucket_pairs(members):
                self._src(i, j).blocking |= kind

        w = self.s.sorted_neighborhood_window
        if w > 1:
            for a in attrs:
                keyed = sorted((v[0], i) for i in idxs
                               for v in [self._norm_values(self.store.records[i], a)] if v)
                for k in range(len(keyed)):
                    for m in range(k + 1, min(k + w, len(keyed))):
                        if keyed[k][1] != keyed[m][1]:
                            self._src(keyed[k][1], keyed[m][1]).blocking |= BLOCK_SORTED

    # ---- exhaustive ----------------------------------------------------------
    def exhaustive_pass(self, idxs: List[int], force: bool = False) -> bool:
        if len(idxs) > self.s.exhaustive_max_entities and not force:
            LOG.add("EXHAUSTIVE_REFUSED", f"Exhaustive mode refused for {len(idxs)} entities "
                                          f"(> EXHAUSTIVE_MAX_ENTITIES={self.s.exhaustive_max_entities})",
                    severity="ERROR")
            return False
        for i, j in itertools.combinations(idxs, 2):
            self._src(i, j).exhaustive = True
        return True

    def run(self, use_tokens: bool = True, use_blocking: Optional[bool] = None,
            exhaustive: Optional[bool] = None) -> Dict[Tuple[int, int], PairSource]:
        t0 = time.time()
        use_blocking = self.s.blocking_enabled if use_blocking is None else use_blocking
        exhaustive = self.s.exhaustive if exhaustive is None else exhaustive
        for etype, idxs in self.store.by_type.items():
            if not any(g.entity_type == etype for g in self.groups):
                continue
            log.info("[%s] candidate generation over %d entities", etype, len(idxs))
            if use_tokens:
                self.token_pass(etype, idxs)
            if use_blocking:
                self.blocking_pass(etype, idxs)
            if exhaustive:
                self.exhaustive_pass(idxs)
        self.stats.seconds = time.time() - t0
        return self.pairs

    def source_counts(self) -> Dict[str, int]:
        c = defaultdict(int)
        for ps in self.pairs.values():
            c["total"] += 1
            if ps.token_groups:
                c["token_logic"] += 1
            elif ps.overcollided_groups:
                c["overcollided_token_only"] += 1
            elif ps.blocking:
                c["extra_blocking_only"] += 1
            elif ps.exhaustive:
                c["exhaustive_only"] += 1
            if ps.blocking:
                c["extra_blocking_any"] += 1
        return dict(c)
