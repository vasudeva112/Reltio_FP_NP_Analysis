"""
Candidate pairs for FN analysis = union of all enabled blocking strategies, minus pairs that are
not "missed matches" by definition:

  * the same entity (same URI)
  * already merged / the same source record (shared crosswalk, merge map)
  * already matched or linked in Reltio (Potential Match Report pairs, known-matches file)

Excluded pairs are counted by reason (Blocking Analysis / FN Summary); they are never evaluated.
An exhaustive mode (every pair, small datasets only) exists to measure blocking recall.
"""

import itertools
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..exceptions_log import LOG
from ..pair_deduplication import pair_key
from ..rule_config_parser import MatchGroup
from ..rule_evaluator import RuleEngine
from .blocking_engine import BlockingEngine, derive_strategies
from .entity_loader import LoadedEntities

log = logging.getLogger("fpnp")


@dataclass
class Candidates:
    pairs: List[Tuple[int, int, int]] = field(default_factory=list)     # (i, j, strategy mask)
    excluded: Counter = field(default_factory=Counter)
    excluded_known: Dict[str, str] = field(default_factory=dict)        # pair key -> reason (known / merged)
    blocking: Optional[BlockingEngine] = None
    total_generated: int = 0


def generate_candidates(loaded: LoadedEntities, groups: Sequence[MatchGroup], engine: RuleEngine, settings,
                        known_pairs: Optional[Dict[str, str]] = None, strategy_kinds: Optional[Sequence[str]] = None,
                        custom_keys: Sequence[str] = (), exhaustive: bool = False,
                        progress: Optional[Callable[[str], None]] = None) -> Candidates:
    known_pairs = known_pairs or {}
    recs = loaded.store.records
    strategies = derive_strategies(groups, strategy_kinds or None, custom_keys, engine)
    be = BlockingEngine(recs, loaded.store.by_type, groups, engine, strategies,
                        max_block_size=getattr(settings, "fn_max_block_size", 500),
                        max_token_collision=getattr(settings, "max_token_collision", 300),
                        sorted_window=getattr(settings, "sorted_neighborhood_window", 10),
                        max_phrases=getattr(settings, "max_phrases_per_entity", 2000))
    pairs = be.run(progress)
    if exhaustive:
        limit = getattr(settings, "exhaustive_max_entities", 5000)
        for et, idxs in loaded.store.by_type.items():
            if len(idxs) > limit:
                LOG.add("EXHAUSTIVE_REFUSED", f"Exhaustive FN comparison refused for {len(idxs)} {et} entities "
                                              f"(> EXHAUSTIVE_MAX_ENTITIES={limit})", severity="ERROR")
                continue
            for i, j in itertools.combinations(idxs, 2):
                k = be.key(i, j)
                pairs.setdefault(k, 0)
    out = Candidates(blocking=be, total_generated=len(pairs))
    for k in sorted(pairs):
        i, j = be.unkey(k)
        a, b = recs[i].uri, recs[j].uri
        if a == b:
            out.excluded["same entity"] += 1
            continue
        pk = pair_key(a, b)
        if pk in loaded.merged_pairs:
            out.excluded["already merged / same source record"] += 1
            out.excluded_known[pk] = loaded.merged_pairs[pk]
            continue
        if pk in known_pairs:
            out.excluded["already matched / linked in Reltio"] += 1
            out.excluded_known[pk] = known_pairs[pk]
            continue
        out.pairs.append((i, j, pairs[k]))
    log.info("Candidate pairs: %d generated, %d to evaluate, excluded %s", len(pairs), len(out.pairs),
             dict(out.excluded))
    return out
