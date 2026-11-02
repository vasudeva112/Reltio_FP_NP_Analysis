"""
Module B - Reltio False Negative analysis over entities.json exports (independent of the
Potential Match Report, which is optional and only used to exclude pairs Reltio already returned).

  Step 1  entity_loader       OV attributes, crosswalks, metadata, merged / same-record detection
  Step 2  candidate_generator blocking strategies (union), exclusions
  Step 3  this module         every candidate pair x every rule of its entity type: comparator engine,
                              attribute scores, overall similarity, rule evaluation
  Step 4  classification      (analytical - never a confirmed FN without steward evidence)

      Confirmed False Negative                    steward MATCH / MERGED and Reltio did not match the pair
      High-confidence potential false negative    a rule is satisfied with documented semantics only, similarity
                                                  >= fn.high_min_score, no null-dependent condition, no conflict
      Medium-confidence potential false negative  a rule is satisfied, similarity >= fn.medium_min_score, but the
                                                  result depends on approximated / null-handling behaviour or
                                                  the similarity is below the high threshold
      Requires manual review                      a rule is satisfied but populated identifiers / dates conflict,
                                                  nested values matched across instances, unsupported classes,
                                                  or the similarity is low (rule passes on nulls)
      Low-confidence candidate                    no rule satisfied, similarity >= fn.low_min_score (near miss -
                                                  input for rule tuning, consistent with Reltio not matching)

Checkpoint / resume: evaluated pair keys and reported results are stored in SQLite after every batch
(existing checkpoint.Checkpoint); an interrupted run continues without re-evaluating any pair.
"""

import hashlib
import json
import logging
import os
import time
import tracemalloc
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..checkpoint import Checkpoint
from ..comparators.base import DOCUMENTED, UNSUPPORTED, EngineContext
from ..exceptions_log import LOG
from ..pair_deduplication import pair_key
from ..rule_config_parser import MatchGroup, parse_match_group
from ..rule_evaluator import RuleEngine
from ..steward_decisions import MERGED, MATCH, NOT_A_MATCH, Decision
from ..reltio_engine.scoring_config import ScoringConfig
from ..reltio_engine.scoring_engine import PairScorer
from .candidate_generator import Candidates, generate_candidates
from .entity_loader import LoadedEntities

log = logging.getLogger("fpnp")

CONFIRMED = "Confirmed False Negative"
HIGH = "High-confidence potential false negative"
MEDIUM = "Medium-confidence potential false negative"
REVIEW = "Requires manual review"
LOW = "Low-confidence candidate"

ACTIONS = {
    "MATCH_EXPECTED_NOT_RETURNED": "Run _verifyMatches for the pair (read-only 'verify' mode). If Reltio says "
                                   "matched=true, rematch the entities; if false, compare its trace with this one.",
    "TOKENIZATION_MISMATCH": "The entities share no match token for this rule, so Reltio never compared them. "
                             "Review the rule's match token classes / ignoreInToken for these attributes.",
    "CANDIDATE_GENERATION_MISS": "The shared token phrase is over-collided (held by more entities than the "
                                 "collision limit) and is skipped by Reltio. Review the token strategy.",
    "RULE_NOT_SATISFIED": "No configured rule is satisfied: consistent with Reltio. If stewards confirm the pair is "
                          "a duplicate, the rule needs tuning (see the failed conditions).",
    "STEWARD_CONFIRMED": "Steward confirmed the duplicate; Reltio did not match it. Investigate the rule trace.",
}


@dataclass
class FNResult:
    pairs: List[dict] = field(default_factory=list)
    attributes: List[dict] = field(default_factory=list)
    blocking: List[dict] = field(default_factory=list)
    largest_blocks: List[dict] = field(default_factory=list)
    stats: Dict[str, object] = field(default_factory=dict)
    config: Optional[ScoringConfig] = None
    rules: List[MatchGroup] = field(default_factory=list)


# ---------------------------------------------------------------------------- worker side
_W: dict = {}


def _init_worker(group_raws, ctx_args, cfg_dict, records, strat_info, min_sim, subprocess=True):
    if subprocess:
        LOG.clear()
    groups = [parse_match_group(g) for g in group_raws]
    engine = RuleEngine(groups, EngineContext(**ctx_args))
    _W.update(groups=groups, engine=engine, scorer=PairScorer(groups, engine, ScoringConfig.from_dict(cfg_dict)),
              records=records, strat=strat_info, min_sim=min_sim, subprocess=subprocess)


def _token_status(mask: int, group_uri: str, strat) -> str:
    shared = over = has = False
    for p, (kind, guri) in enumerate(strat):
        if guri != group_uri:
            continue
        if kind == "reltio_tokens":
            has = True
            shared |= bool(mask >> p & 1)
        elif kind == "reltio_tokens_overcollided":
            over |= bool(mask >> p & 1)
    return "SHARED" if shared else "OVERCOLLIDED_ONLY" if over else "NOT_SHARED" if has else "UNKNOWN"


def _eval_batch(batch):
    scorer: PairScorer = _W["scorer"]
    recs = _W["records"]
    out = []
    for i, j, mask in batch:
        L, R = recs[i], recs[j]
        key = pair_key(L.uri, R.uri)
        scores = scorer.score_all(L, R, cache=True)
        if not scores:
            out.append((key, None, mask, False))
            continue
        matched = [s for s in scores if s.outcome.matched]
        best = PairScorer.best(scores)
        any_match = bool(matched)
        if not any_match and (best.overall is None or best.overall < _W["min_sim"]):
            out.append((key, None, mask, False))
            continue
        ev = best.evaluation
        payload = {
            "i": i, "j": j, "mask": mask, "summary": best.summary(), "attrs": best.attribute_rows(),
            "satisfied": [s.group.label for s in matched], "overall": best.overall, "matched": best.outcome.matched,
            "status": best.outcome.status, "fidelity": best.outcome.fidelity,
            "failed": best.outcome.failed_conditions, "null_satisfied": best.null_satisfied,
            "conflicts": best.conflicts, "cross": best.cross_instance, "unmatched": best.unmatched_attributes,
            "other_rule_conflicts": sorted({f"{c} in {s.group.name}" for s in scores if s is not best
                                            for c in s.conflicts}),
            "approx": [l.attribute.rsplit("/", 1)[-1] for l in ev.leaves() if l.passed and l.status != DOCUMENTED],
            "group_uri": best.group.uri,
            "token": _token_status(mask, best.group.uri, _W["strat"]),
        }
        out.append((key, payload, mask, any_match))
    _W["engine"].clear_cache()
    snap = {}
    if _W.get("subprocess"):
        snap = LOG.snapshot()
        LOG.clear()
    return out, snap


# ---------------------------------------------------------------------------- analyser
class ReltioFNAnalyzer:
    def __init__(self, groups: Sequence[MatchGroup], engine: RuleEngine, loaded: LoadedEntities,
                 config: Optional[ScoringConfig] = None, settings=None,
                 known_pairs: Optional[Dict[str, str]] = None, steward: Optional[Dict[str, Decision]] = None):
        self.groups = list(groups)
        self.engine = engine
        self.loaded = loaded
        self.cfg = config or ScoringConfig()
        self.s = settings
        self.known = known_pairs or {}
        self.steward = steward or {}

    def _setting(self, name, default):
        return getattr(self.s, name, default) if self.s is not None else default

    def signature(self, strategies: Sequence[str]) -> str:
        h = hashlib.sha256()
        h.update(json.dumps([g.raw for g in self.groups], sort_keys=True).encode())
        h.update(json.dumps(self.cfg.to_dict(), sort_keys=True).encode())
        h.update(json.dumps(list(strategies)).encode())
        h.update(str(len(self.loaded.store)).encode())
        ctx = self.engine.ctx
        h.update(f"{ctx.case_insensitive}{ctx.treat_blank_as_null}{ctx.relevance_constraint_mode}"
                 f"{ctx.relevance_nested_aggregation}{ctx.both_null_exact_mode}"
                 f"{self._setting('fn_min_similarity', 60.0)}".encode())
        return h.hexdigest()

    def run(self, out_dir: str = "", resume: bool = True, exhaustive: bool = False,
            progress: Optional[Callable[[str, float], None]] = None) -> FNResult:
        t0 = time.time()
        tracemalloc.start()
        say = (lambda msg, frac=0.0: progress(msg, frac)) if progress else (lambda msg, frac=0.0: None)
        say("Generating candidate pairs", 0.05)
        cand = generate_candidates(self.loaded, self.groups, self.engine, self.s, self.known,
                                   self._setting("fn_blocking_strategies", []) or None,
                                   self._setting("fn_custom_blocking_keys", []), exhaustive,
                                   progress=lambda m: say(m, 0.1))
        be = cand.blocking
        strat_info = [(s.kind, s.group_uri) for s in be.strategies]
        min_sim = min(float(self._setting("fn_min_similarity", 60.0)), self.cfg.fn.low_min_score)

        cp_path = os.path.join(out_dir or ".", "fn_checkpoint.sqlite")
        cp = Checkpoint(cp_path, self.signature([s.name for s in be.strategies]), resume)
        try:
            done = cp.evaluated_keys() if resume else set()
            recs = self.loaded.store.records
            todo = [(i, j, m) for i, j, m in cand.pairs if pair_key(recs[i].uri, recs[j].uri) not in done]
            counts = Counter(cp.load_stats("strategy_counts") or {})
            log.info("FN pairs to evaluate: %d (resume skipped %d)", len(todo), len(done))
            bs = max(1, int(self._setting("batch_size", 5000)))
            batches = [todo[k:k + bs] for k in range(0, len(todo), bs)]
            workers = int(self._setting("workers", 1))
            group_raws = [g.raw for g in self.groups]
            ctx = self.engine.ctx
            ctx_args = dict(case_insensitive=ctx.case_insensitive, treat_blank_as_null=ctx.treat_blank_as_null,
                            noise_dictionaries=ctx.noise_dictionaries, name_dictionary=ctx.name_dictionary,
                            regexp_rules=ctx.regexp_rules, relevance_constraint_mode=ctx.relevance_constraint_mode,
                            relevance_nested_aggregation=ctx.relevance_nested_aggregation,
                            double_metaphone_max_length=ctx.double_metaphone_max_length,
                            both_null_exact_mode=ctx.both_null_exact_mode)

            def absorb(batch, results):
                rows = []
                for key, payload, mask, any_match in results:
                    if any_match or payload is not None:
                        for p in range(mask.bit_length()):
                            if mask >> p & 1:
                                if any_match:
                                    counts[f"m{p}"] += 1
                                if payload is not None:
                                    counts[f"r{p}"] += 1
                    if payload is not None:
                        rows.append((key, "best", payload))
                cp.save_batch((pair_key(recs[i].uri, recs[j].uri) for i, j, _ in batch), rows)
                cp.save_stats("strategy_counts", dict(counts))

            if workers > 1 and len(todo) > bs:
                with ProcessPoolExecutor(workers, initializer=_init_worker,
                                         initargs=(group_raws, ctx_args, self.cfg.to_dict(), recs, strat_info,
                                                   min_sim, True)) as pool:
                    for n, (results, snap) in enumerate(pool.map(_eval_batch, batches), 1):
                        absorb(batches[n - 1], results)
                        LOG.merge_snapshot(snap)
                        say(f"Evaluated batch {n}/{len(batches)}", 0.15 + 0.75 * n / len(batches))
            else:
                _W.clear()
                _W.update(groups=self.groups, engine=self.engine, records=recs, strat=strat_info, min_sim=min_sim,
                          scorer=PairScorer(self.groups, self.engine, self.cfg), subprocess=False)
                for n, batch in enumerate(batches, 1):
                    results, _ = _eval_batch(batch)
                    absorb(batch, results)
                    say(f"Evaluated batch {n}/{len(batches)}", 0.15 + 0.75 * n / max(1, len(batches)))

            for p, s in enumerate(be.strategies):
                s.stats["matching_candidates"] = counts.get(f"m{p}", 0)
                s.stats["reported_candidates"] = counts.get(f"r{p}", 0)
            say("Classifying candidates", 0.92)
            res = self._classify(cp, be, cand)
        finally:
            cp.close()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        res.stats.update({
            **self.loaded.stats, "candidate_pairs": cand.total_generated, "pairs_evaluated": len(cand.pairs),
            "pairs_resumed": len(done), "excluded": dict(cand.excluded),
            "blocking_seconds": round(be.seconds, 1), "seconds": round(time.time() - t0, 1),
            "peak_memory_mb_python_heap": round(peak / 1e6, 1), "strategies": len(be.strategies),
            "checkpoint": cp_path,
        })
        say("Done", 1.0)
        return res

    # ------------------------------------------------------------------
    def classify_one(self, p: dict, st: Optional[Decision]) -> Tuple[Optional[str], str, str]:
        f = self.cfg.fn
        overall = p["overall"]
        if st is not None and st.kind == NOT_A_MATCH:
            return None, "", f"steward decision {st.raw}"
        if st is not None and st.kind in (MATCH, MERGED):
            return CONFIRMED, "STEWARD_CONFIRMED", f"Steward decision {st.raw}; Reltio did not match the pair."
        if p["matched"]:
            token = p["token"]
            code = {"SHARED": "MATCH_EXPECTED_NOT_RETURNED", "OVERCOLLIDED_ONLY": "CANDIDATE_GENERATION_MISS",
                    "NOT_SHARED": "TOKENIZATION_MISMATCH"}.get(token, "MATCH_EXPECTED_NOT_RETURNED")
            text = {
                "MATCH_EXPECTED_NOT_RETURNED": "The pair satisfies the rule in Python and shares the rule's match "
                                               "tokens, so Reltio should have compared it. Possible causes: data "
                                               "changed after the last match run (rematch), the rule was changed or "
                                               "inactive at match time, or a comparator / cleanser behaves "
                                               "differently on the tenant.",
                "CANDIDATE_GENERATION_MISS": "The pair satisfies the rule but its only shared token phrase is "
                                             "over-collided, so Reltio skips it during candidate selection.",
                "TOKENIZATION_MISMATCH": "The pair satisfies the rule but shares no match token for it, so Reltio "
                                         "never selected the two entities as candidates.",
            }[code]
            extras = []
            if p["null_satisfied"]:
                extras.append("Rule satisfied partly by null handling on: " + ", ".join(p["null_satisfied"]) + ".")
            if p["fidelity"] != DOCUMENTED:
                extras.append(f"Decisive conditions use {p['fidelity'].lower()} Python behaviour"
                              + (f" ({', '.join(dict.fromkeys(p['approx']))})" if p["approx"] else "") + ".")
            other = p.get("other_rule_conflicts") or []
            if p["conflicts"] or p["cross"]:
                extras.append("Conflicting populated values: " + ", ".join(p["conflicts"] + p["cross"]) + ".")
            if other:
                extras.append("Other configured rules see conflicting values: " + ", ".join(other) + ".")
            if p["unmatched"]:
                extras.append("Rule satisfied although these compared attributes differ: "
                              + ", ".join(p["unmatched"]) + ".")
            text = " ".join([text] + extras)
            if p["fidelity"] == UNSUPPORTED or p["conflicts"] or p["cross"] or other:
                return REVIEW, code, text
            if p["unmatched"] and p["null_satisfied"]:
                return REVIEW, code, text + " Match rests on null handling while compared values differ."
            if overall is None or overall < f.medium_min_score:
                return REVIEW, code, text + f" Estimated similarity only {overall if overall is not None else 'n/a'}."
            if (p["fidelity"] == DOCUMENTED and not p["null_satisfied"] and not p["unmatched"]
                    and overall >= f.high_min_score):
                return HIGH, code, text
            return MEDIUM, code, text
        if overall is not None and overall >= f.low_min_score:
            return LOW, "RULE_NOT_SATISFIED", (
                f"Estimated similarity {overall:.1f}, but no configured rule is satisfied (best rule fails on: "
                + (", ".join(p["failed"]) or "threshold") + "). Consistent with Reltio not matching; a near miss "
                "worth steward review if the records look like the same entity.")
        return None, "", "below thresholds"

    def _classify(self, cp: Checkpoint, be, cand: Candidates) -> FNResult:
        res = FNResult(config=self.cfg, rules=self.groups)
        meta = self.loaded.meta
        recs = self.loaded.store.records
        excluded = Counter()
        for key, _, p in cp.results():
            st = self.steward.get(key)
            cls, code, text = self.classify_one(p, st)
            if cls is None:
                excluded["steward NOT_A_MATCH" if st is not None and st.kind == NOT_A_MATCH else "below thresholds"] += 1
                continue
            i, j = p["i"], p["j"]
            if i >= len(recs) or j >= len(recs) or pair_key(recs[i].uri, recs[j].uri) != key:
                continue                                    # stale checkpoint entry (store changed)
            sm = p["summary"]
            ma, mb = meta[i], meta[j]
            row = {
                "PairID": key, "EntityA": sm["EntityA"], "EntityB": sm["EntityB"], "EntityType": sm["EntityType"],
                "ApplicableMatchRule": sm["MatchRule"], "ApplicableMatchRuleURI": sm["MatchRuleURI"],
                "RuleType": sm["RuleType"], "RulesSatisfied": ", ".join(p["satisfied"]),
                "OverallSimilarityScore": sm["EstimatedSimilarityScore"], "SimilarityGroup": sm["SimilarityGroup"],
                "RuleEvaluation": sm["RuleEvaluation"], "PythonRelevance": sm["PythonRelevance"],
                "FNConfidence": cls, "MissedMatchReasonCode": code, "ReasonForPotentialMissedMatch": text,
                "RecommendedAction": ACTIONS.get(code, ""),
                "ReltioTokenStatus": p["token"], "BlockingStrategies": ", ".join(be.names(p["mask"])),
                "MatchedAttributes": sm["MatchedAttributes"], "UnmatchedAttributes": sm["UnmatchedAttributes"],
                "NullAttributes": sm["NullAttributes"], "Conflicts": sm["Conflicts"],
                "TopContributors": sm["TopContributors"], "RuleFidelity": sm["RuleFidelity"],
                "EntityA_Crosswalks": " | ".join(ma.crosswalk_keys[:10]),
                "EntityB_Crosswalks": " | ".join(mb.crosswalk_keys[:10]),
                "EntityA_Updated": ma.updated, "EntityB_Updated": mb.updated,
                "EntityA_File": ma.source_file, "EntityB_File": mb.source_file,
                "StewardDecision": st.raw if st else "", "RuleExplanation": sm["RuleExplanation"],
            }
            res.pairs.append(row)
            for a in p["attrs"]:
                a["EntityA"], a["EntityB"] = sm["EntityA"], sm["EntityB"]
                res.attributes.append(a)
        order = {c: n for n, c in enumerate([CONFIRMED, HIGH, MEDIUM, REVIEW, LOW])}
        res.pairs.sort(key=lambda r: (order.get(r["FNConfidence"], 9), -(r["OverallSimilarityScore"] or 0)))
        res.blocking = be.analysis_rows()
        res.largest_blocks = be.largest_block_rows()
        res.stats["not_reported"] = dict(excluded)
        return res
