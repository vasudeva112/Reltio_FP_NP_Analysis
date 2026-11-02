"""
Step 2 of FN analysis - scalable candidate generation by blocking (no all-pairs comparison).

Strategies are derived from the match-rule configuration (nothing is hardcoded) and can be
switched on / off individually. A candidate pair is the UNION over all enabled strategies, so a
pair missed by one blocking key is still found by any other strategy that groups it.

  reltio_tokens   Reltio-style match-token phrases per rule (existing GroupTokenizer: token classes,
                  ignoreInToken, AND concatenation, strict filters). Phrases held by more than
                  MAX_TOKEN_COLLISION entities are recorded under reltio_tokens_overcollided - Reltio
                  skips those, so pairs found only there are candidate-generation misses.
  rule_keys       per rule: composite key of every attribute the rule compares with exact/fuzzy
                  (normalised exact values)
  rule_phonetic   per rule: same attributes, phonetic / fuzzy-compared text attributes reduced to their
                  Double Metaphone code (catches spelling variants)
  single          every compared attribute on its own (large blocks such as gender skipped + reported)
  pairs           every pair of attributes compared by the same rule
  sorted          sorted neighbourhood per attribute (window SORTED_NEIGHBORHOOD_WINDOW)
                  ("single" and "sorted" are opt-in: FN_BLOCKING_STRATEGIES; see DEFAULT_KINDS)
  custom          user keys, e.g. "LastName+DoB", "FirstName:phonetic+LastName", "Identifiers.Value:digits",
                  "Name:prefix4", "DoB:year" (attributes by dotted name, transforms: exact, phonetic,
                  digits, prefixN, year, lower)

Per strategy the Blocking Analysis reports: entities with a key, blocks, oversized blocks skipped,
candidate pairs generated, pairs only this strategy found, and (after evaluation) matching candidates.
"""

import itertools
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..comparators.base import EngineContext
from ..comparators.phonetic import double_metaphone
from ..exceptions_log import LOG
from ..match_tokens import GroupTokenizer
from ..rule_config_parser import CONSTRAINT_OPS, MatchGroup, Node, attr_uri_from_dotted, short_attr
from ..rule_evaluator import RuleEngine
from ..reltio_engine.comparator_engine import comparator_kind

log = logging.getLogger("fpnp")

ALL_KINDS = ("reltio_tokens", "rule_keys", "rule_phonetic", "single", "pairs", "sorted", "custom")
# default set: selective keys that scale to millions of entities. "single" (one attribute) and "sorted"
# (neighbourhood) raise recall for typo-level differences at a much higher pair count - enable them explicitly.
DEFAULT_KINDS = ("reltio_tokens", "rule_keys", "rule_phonetic", "pairs", "custom")
MAX_VALUES = 5            # values per attribute used for keys
MAX_KEYS = 50             # keys per entity per strategy (multi-value products)
KEY_SEP = "\u241f"         # separator between attribute parts of a composite key


@dataclass
class BlockingStrategy:
    name: str
    kind: str
    entity_type: str
    attributes: List[str]
    transforms: List[str]
    group_uri: str = ""
    description: str = ""
    stats: Dict[str, int] = field(default_factory=lambda: {
        "entities_with_key": 0, "blocks": 0, "blocks_with_pairs": 0, "oversized_blocks_skipped": 0,
        "entities_in_skipped_blocks": 0, "largest_block": 0, "candidate_pairs": 0, "unique_pairs_only_here": 0,
        "matching_candidates": 0, "reported_candidates": 0})
    largest: List[Tuple[int, str, bool]] = field(default_factory=list)   # (size, key, skipped)

    @property
    def key_label(self) -> str:
        if self.kind == "reltio_tokens" or self.kind == "reltio_tokens_overcollided":
            return f"match tokens of {self.group_uri.rsplit('/', 1)[-1]}"
        return " + ".join(f"{short_attr(a)}" + (f":{t}" if t != "exact" else "")
                          for a, t in zip(self.attributes, self.transforms))


def _positive_compared_ops(g: MatchGroup) -> List[Node]:
    """exact / fuzzy operands not under NOT / OR (the attributes a duplicate must agree on)."""
    out: List[Node] = []

    def walk(n: Node, blocked: bool) -> None:
        if n.kind == "op":
            if not blocked and n.op in ("exact", "fuzzy"):
                out.append(n)
            return
        for c in n.children:
            walk(c, blocked or n.kind in ("not", "or"))
    walk(g.root, False)
    return out


def _compared_attrs(g: MatchGroup) -> List[str]:
    """Every attribute compared by any exact / fuzzy / exactOr* operand (incl. under OR), no filters."""
    out = []
    for n in g.root.walk():
        if n.kind == "op" and n.op not in CONSTRAINT_OPS and n.op != "multi" and not n.op.startswith("not"):
            if n.attribute not in out:
                out.append(n.attribute)
        elif n.kind == "op" and n.op == "multi":
            out.extend(a for a in n.attributes if a not in out)
    return out


def parse_custom_key(spec: str, entity_type: str) -> Tuple[List[str], List[str]]:
    attrs, trans = [], []
    for part in spec.split("+"):
        part = part.strip()
        if not part:
            continue
        name, _, t = part.partition(":")
        uri = name if name.startswith("configuration/") else attr_uri_from_dotted(entity_type, name.strip())
        attrs.append(uri)
        trans.append((t or "exact").strip().lower())
    return attrs, trans


def derive_strategies(groups: Sequence[MatchGroup], kinds: Optional[Sequence[str]] = None,
                      custom: Sequence[str] = (), engine: Optional[RuleEngine] = None) -> List[BlockingStrategy]:
    kinds = set(kinds or DEFAULT_KINDS)
    unknown = kinds - set(ALL_KINDS)
    if unknown:
        raise ValueError(f"Unknown blocking strategies {sorted(unknown)}; choose from {ALL_KINDS}")
    out: List[BlockingStrategy] = []
    seen: Set[Tuple] = set()

    def add(s: BlockingStrategy) -> None:
        # identical keys derived from several rules are generated once (group only matters for token phrases)
        sig = (s.kind, s.entity_type, tuple(s.attributes), tuple(s.transforms),
               s.group_uri if s.kind.startswith("reltio_tokens") else "")
        if sig not in seen:
            seen.add(sig)
            out.append(s)

    for g in groups:
        et, rn = g.entity_type, g.name
        if "reltio_tokens" in kinds:
            add(BlockingStrategy(f"reltio_tokens:{rn}", "reltio_tokens", et, [], [], g.uri,
                                 "Reltio-style match-token phrases of the rule"))
            add(BlockingStrategy(f"reltio_tokens_overcollided:{rn}", "reltio_tokens_overcollided", et, [], [], g.uri,
                                 "Shared phrases above MAX_TOKEN_COLLISION (Reltio skips these)"))
        ops = _positive_compared_ops(g)
        attrs = list(dict.fromkeys(n.attribute for n in ops))
        if attrs and "rule_keys" in kinds:
            add(BlockingStrategy(f"rule_keys:{rn}", "rule_keys", et, attrs, ["exact"] * len(attrs), g.uri,
                                 "All exact/fuzzy-compared attributes of the rule, exact values"))
        if attrs and "rule_phonetic" in kinds:
            trans = []
            for a in attrs:
                cm = g.comparators.get(a)
                k = comparator_kind(cm.short if cm else "BasicStringComparator")
                trans.append("phonetic" if k in ("phonetic", "fuzzy") else "exact")
            if "phonetic" in trans:
                add(BlockingStrategy(f"rule_phonetic:{rn}", "rule_phonetic", et, attrs, trans, g.uri,
                                     "Rule attributes; fuzzy/phonetic ones by Double Metaphone code"))
        compared = _compared_attrs(g)
        if "single" in kinds:
            for a in compared:
                add(BlockingStrategy(f"single:{short_attr(a)}", "single", et, [a], ["exact"], "",
                                     "One compared attribute, exact value"))
        if "pairs" in kinds:
            for a, b in itertools.combinations(compared, 2):
                a, b = sorted((a, b))
                add(BlockingStrategy(f"pairs:{short_attr(a)}+{short_attr(b)}", "pairs", et, [a, b],
                                     ["exact", "exact"], "", "Two attributes compared by the same rule"))
        if "sorted" in kinds:
            for a in compared:
                add(BlockingStrategy(f"sorted:{short_attr(a)}", "sorted", et, [a], ["lower"], "",
                                     "Sorted neighbourhood on the attribute"))
    if "custom" in kinds:
        for et in sorted({g.entity_type for g in groups}):
            for spec in custom:
                attrs, trans = parse_custom_key(spec, et)
                if attrs:
                    add(BlockingStrategy(f"custom:{spec}", "custom", et, attrs, trans, "", "User-defined key"))
    return out


def _transform(v: str, t: str) -> str:
    if t in ("exact", "lower"):
        return v
    if t == "phonetic":
        codes = double_metaphone(v)
        return codes[0] if codes else v
    if t == "digits":
        return re.sub(r"\D", "", v)
    if t == "year":
        m = re.search(r"(\d{4})", v)
        return m.group(1) if m else ""
    m = re.fullmatch(r"prefix(\d+)", t)
    if m:
        return v[: int(m.group(1))]
    raise ValueError(f"Unknown blocking transform '{t}'")


class BlockingEngine:
    """Generates candidate pairs (index pairs into the entity store) with provenance bitmasks."""

    def __init__(self, records, by_type: Dict[str, List[int]], groups: Sequence[MatchGroup], engine: RuleEngine,
                 strategies: List[BlockingStrategy], max_block_size: int = 500, max_token_collision: int = 300,
                 sorted_window: int = 10, max_phrases: int = 2000):
        self.records = records
        self.by_type = by_type
        self.groups = {g.uri: g for g in groups}
        self.engine = engine
        self.ctx: EngineContext = engine.ctx
        self.strategies = strategies
        self.max_block = max_block_size
        self.max_collision = max_token_collision
        self.window = sorted_window
        self.max_phrases = max_phrases
        self.pairs: Dict[int, int] = {}                  # (i << 32 | j) -> strategy bitmask
        self.seconds = 0.0

    @staticmethod
    def key(i: int, j: int) -> int:
        return (i << 32) | j if i < j else (j << 32) | i

    @staticmethod
    def unkey(k: int) -> Tuple[int, int]:
        return k >> 32, k & 0xFFFFFFFF

    def _values(self, rec, attr: str) -> List[str]:
        vals = [self.ctx.norm(v) for v in rec.get(attr).values]
        return sorted({v for v in vals if v})[:MAX_VALUES]

    def _keys(self, rec, s: BlockingStrategy) -> Set[str]:
        per_attr = []
        for a, t in zip(s.attributes, s.transforms):
            vals = {x for x in (_transform(v, t) for v in self._values(rec, a)) if x}
            if not vals:
                return set()
            per_attr.append(sorted(vals))
        out = set()
        for combo in itertools.product(*per_attr):
            out.add("␟".join(combo))
            if len(out) >= MAX_KEYS:
                break
        return out

    def _emit_block(self, s: BlockingStrategy, bit: int, key: str, members: List[int], limit: int) -> None:
        n = len(members)
        if n < 2:
            return
        s.stats["largest_block"] = max(s.stats["largest_block"], n)
        if n > limit:
            s.stats["oversized_blocks_skipped"] += 1
            s.stats["entities_in_skipped_blocks"] += n
            s.largest.append((n, key, True))
            return
        s.stats["blocks_with_pairs"] += 1
        s.largest.append((n, key, False))
        for i, j in itertools.combinations(members, 2):
            k = self.key(i, j)
            s.stats["candidate_pairs"] += 1
            self.pairs[k] = self.pairs.get(k, 0) | bit

    def run(self, progress: Optional[Callable[[str], None]] = None) -> Dict[int, int]:
        t0 = time.time()
        for bit_pos, s in enumerate(self.strategies):
            bit = 1 << bit_pos
            idxs = self.by_type.get(s.entity_type, [])
            if len(idxs) < 2:
                continue
            if progress:
                progress(f"blocking {s.name}")
            if s.kind == "reltio_tokens_overcollided":
                continue                                   # filled together with reltio_tokens
            if s.kind == "reltio_tokens":
                self._token_strategy(s, bit, idxs)
            elif s.kind == "sorted":
                self._sorted_strategy(s, bit, idxs)
            else:
                buckets: Dict[str, List[int]] = defaultdict(list)
                for i in idxs:
                    ks = self._keys(self.records[i], s)
                    if ks:
                        s.stats["entities_with_key"] += 1
                    for k in ks:
                        buckets[k].append(i)
                s.stats["blocks"] = len(buckets)
                for k, members in buckets.items():
                    self._emit_block(s, bit, k, members, self.max_block)
            s.largest = sorted(s.largest, reverse=True)[:10]
        self._only_here()
        self.seconds = time.time() - t0
        return self.pairs

    def _token_strategy(self, s: BlockingStrategy, bit: int, idxs: List[int]) -> None:
        g = self.groups[s.group_uri]
        over = next((x for x in self.strategies if x.kind == "reltio_tokens_overcollided"
                     and x.group_uri == s.group_uri), None)
        over_bit = 1 << self.strategies.index(over) if over else 0
        tok = GroupTokenizer(g, self.ctx, self.max_phrases)
        buckets: Dict[str, List[int]] = defaultdict(list)
        for i in idxs:
            phrases = tok.phrases(self.engine.prepare(g, self.records[i]))
            if phrases:
                s.stats["entities_with_key"] += 1
            for p in phrases:
                buckets[p].append(i)
        s.stats["blocks"] = len(buckets)
        for p, members in buckets.items():
            if len(members) < 2:
                continue
            if len(members) > self.max_collision:
                if over is not None:
                    over.stats["entities_with_key"] += len(members)
                    over.stats["blocks"] += 1
                    self._emit_block(over, over_bit, p, members, max(self.max_block, self.max_collision))
                continue
            self._emit_block(s, bit, p, members, self.max_collision)

    def _sorted_strategy(self, s: BlockingStrategy, bit: int, idxs: List[int]) -> None:
        a = s.attributes[0]
        keyed = []
        for i in idxs:
            vals = self._values(self.records[i], a)
            if vals:
                s.stats["entities_with_key"] += 1
                keyed.append((vals[0], i))
        keyed.sort()
        s.stats["blocks"] = len(keyed)
        w = max(2, self.window)
        for k in range(len(keyed)):
            for m in range(k + 1, min(k + w, len(keyed))):
                i, j = keyed[k][1], keyed[m][1]
                if i != j:
                    kk = self.key(i, j)
                    s.stats["candidate_pairs"] += 1
                    self.pairs[kk] = self.pairs.get(kk, 0) | bit

    def _only_here(self) -> None:
        for mask in self.pairs.values():
            if mask & (mask - 1) == 0:                      # exactly one bit set
                pos = mask.bit_length() - 1
                self.strategies[pos].stats["unique_pairs_only_here"] += 1

    def names(self, mask: int) -> List[str]:
        return [s.name for p, s in enumerate(self.strategies) if mask >> p & 1]

    def token_status(self, mask: int, group_uri: str) -> str:
        """SHARED (Reltio would compare) / OVERCOLLIDED_ONLY / NOT_SHARED for one rule."""
        shared = over = False
        for p, s in enumerate(self.strategies):
            if s.group_uri == group_uri and mask >> p & 1:
                if s.kind == "reltio_tokens":
                    shared = True
                elif s.kind == "reltio_tokens_overcollided":
                    over = True
        if shared:
            return "SHARED"
        if over:
            return "OVERCOLLIDED_ONLY"
        has_token_strategy = any(s.kind == "reltio_tokens" and s.group_uri == group_uri for s in self.strategies)
        return "NOT_SHARED" if has_token_strategy else "UNKNOWN"

    def analysis_rows(self) -> List[dict]:
        rows = []
        for s in self.strategies:
            st = s.stats
            rows.append({
                "BlockingStrategy": s.kind, "StrategyName": s.name, "EntityType": s.entity_type,
                "BlockingKey": s.key_label, "Description": s.description,
                "NumberOfEntities": st["entities_with_key"], "Blocks": st["blocks"],
                "BlocksWithPairs": st["blocks_with_pairs"], "LargestBlock": st["largest_block"],
                "OversizedBlocksSkipped": st["oversized_blocks_skipped"],
                "EntitiesInSkippedBlocks": st["entities_in_skipped_blocks"],
                "CandidatePairsGenerated": st["candidate_pairs"],
                "PairsFoundOnlyByThisStrategy": st["unique_pairs_only_here"],
                "MatchingCandidates": st["matching_candidates"],
                "ReportedFNCandidates": st["reported_candidates"],
            })
        return rows

    def largest_block_rows(self, top: int = 25) -> List[dict]:
        rows = []
        for s in self.strategies:
            for n, k, skipped in s.largest:
                rows.append({"StrategyName": s.name, "BlockSize": n, "Skipped": skipped,
                             "KeyValue": k.replace(KEY_SEP, " | ")[:200]})
        return sorted(rows, key=lambda r: -r["BlockSize"])[:top]
