"""
Component C - false negative detection over the full entity export.

Every candidate pair (token logic / extra blocking / exhaustive) is evaluated against
every match group of its entity type. Pairs that satisfy a group in Python are then
compared with Reltio output and classified:

  Reltio Match                 pair present in the Potential Match Report (any group)
  Confirmed False Negative     steward MATCH / MERGED and Reltio did not return the pair
  Excluded / Unclassified      steward NOT_A_MATCH (or negative rule applied)
  Requires Steward Review      tenant verification confirms Reltio does not match the pair
  Suspected False Negative     decisive conditions use only documented/verified behaviour
  Python-Detected Candidate    decisive conditions depend on an approximation
"""

import hashlib
import json
import logging
import time
import tracemalloc
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Optional, Tuple

from .candidate_generation import CandidateGenerator, EntityStore, PairSource
from .checkpoint import Checkpoint
from .comparators.base import APPROXIMATION, DOCUMENTED, EngineContext
from .exceptions_log import LOG
from .explain import per_attribute_results, value_columns
from .pair_deduplication import pair_key
from .rule_config_parser import MatchGroup, parse_match_group, short_attr as short
from .rule_evaluator import GroupEvaluation, RuleEngine
from .steward_decisions import NOT_A_MATCH, Decision

log = logging.getLogger("fpnp")

CONFIRMED_FN = "Confirmed False Negative"
SUSPECTED_FN = "Suspected False Negative"
PY_CANDIDATE = "Python-Detected Candidate"
RELTIO_MATCH = "Reltio Match"
REVIEW = "Requires Steward Review"
EXCLUDED = "Excluded / Unclassified"

NEXT_ACTION = {
    "TOKENIZATION_MISMATCH": "Run _verifyMatches for the pair: if matchTokens.intersection is empty, the token "
                             "classes / ignoreInToken prevent Reltio from ever comparing it.",
    "CANDIDATE_GENERATION_MISS": "Shared token phrase is over-collided (> MAX_TOKEN_COLLISION profiles); "
                                 "confirm with _verifyMatches (checkOvercollisionedTokens) and review the token "
                                 "strategy.",
    "NULL_HANDLING_MISMATCH": "Verify exactOrNull/exactOrAllNull behaviour for this pair with _verifyMatches.",
    "COMPARATOR_BEHAVIOR_MISMATCH": "Verify the comparator result on the tenant (Compare Strings API / "
                                    "_verifyMatches); Python comparator is an approximation.",
    "CLEANSER_MISMATCH": "Check the cleanser output on the tenant; dictionary not available locally.",
    "RULE_LOGIC_MISMATCH": "Run _verifyMatches; if Reltio says matched=false with common tokens, compare the "
                           "rule trace attribute by attribute.",
    "NEEDS_STEWARD_REVIEW": "Steward review: tenant confirms Reltio does not match the pair.",
    "INSUFFICIENT_GROUND_TRUTH": "Collect a steward decision for the pair.",
}


# ----------------------------------------------------------------------
def _evaluate_pair(engine: RuleEngine, groups: List[MatchGroup], store_records, i: int, j: int) -> List[GroupEvaluation]:
    L, R = store_records[i], store_records[j]
    return [engine.evaluate(g, L, R, cache=True) for g in groups if g.entity_type == L.entity_type]


def _result_payload(ev: GroupEvaluation, L, R, src: PairSource, g: MatchGroup) -> dict:
    d = {
        "entity1": L.uri, "entity2": R.uri, "entity_type": L.entity_type,
        "group_uri": g.uri, "group_label": g.label, "group_type": g.type,
        "matched": ev.matched, "relevance": ev.relevance,
        "action": f"{ev.action_type}:{ev.action_label}" if ev.action_type else "",
        "applicable": ev.applicable, "negative_rule_hit": ev.negative_rule_hit,
        "decisive_status": ev.decisive_status(),
        "contributing": ev.contributing(), "failed": ev.failed(), "differing": ev.differing(),
        "nulls": ev.null_contributions(), "cross_instance": ev.cross_instance_attrs,
        "per_attribute": per_attribute_results(ev),
        "values": value_columns(ev, "L_", "R_"),
        "trace_text": ev.trace_text(), "trace_json": ev.trace_json(),
        "source": src.as_dict(), "source_label": src.label(g.uri), "blocking": src.blocking_names(),
        "approx_leaves": [l.attribute for l in ev.leaves() if l.passed and l.status != DOCUMENTED],
        "null_leaves": [l.attribute for l in ev.leaves() if l.passed and l.null_case],
        "permissive_leaves": [l.attribute for l in ev.leaves() if l.permissive_hit],
        "cleansed_leaves": [l.attribute for l in ev.leaves()
                            if l.passed and any(n.endswith((":mapped", ":applied")) for n in l.cleanse_notes)],
    }
    return d


# ---- worker process support ------------------------------------------------
_W: dict = {}


def _worker_init(group_raws, settings_ctx, records):
    from .exceptions_log import LOG as wlog
    wlog.clear()
    groups = [parse_match_group(g) for g in group_raws]
    _W["groups"] = groups
    _W["engine"] = RuleEngine(groups, EngineContext(**settings_ctx))
    _W["records"] = records
    _W["subprocess"] = True


def _worker_batch(batch):
    from .exceptions_log import LOG as wlog
    engine, groups, records = _W["engine"], _W["groups"], _W["records"]
    by_uri = {g.uri: g for g in groups}
    results, counts = [], Counter()
    for i, j, src_d in batch:
        src = PairSource.from_dict(src_d)
        for ev in _evaluate_pair(engine, groups, records, i, j):
            counts[(ev.group_uri, "evaluated")] += 1
            if not ev.applicable:
                counts[(ev.group_uri, "not_applicable")] += 1
            if ev.negative_rule_hit:
                counts[(ev.group_uri, "negative_rule_hit")] += 1
            if ev.matched:
                counts[(ev.group_uri, "satisfied")] += 1
                results.append((pair_key(records[i].uri, records[j].uri), ev.group_uri,
                                _result_payload(ev, records[i], records[j], src, by_uri[ev.group_uri])))
    engine.clear_cache()
    snap = {}
    if _W.get("subprocess"):                     # ship worker exceptions back to the parent
        snap = wlog.snapshot()
        wlog.clear()
    return results, dict(counts), snap


# ----------------------------------------------------------------------
class FalseNegativeAnalyzer:
    def __init__(self, groups: List[MatchGroup], engine: RuleEngine, settings, store: EntityStore,
                 report_pairs: Dict[str, set], steward: Dict[str, Decision],
                 verification: Optional[Dict[str, dict]] = None):
        self.groups = groups
        self.by_uri = {g.uri: g for g in groups}
        self.engine = engine
        self.s = settings
        self.store = store
        self.report_pairs = report_pairs
        self.steward = steward
        self.verification = verification or {}
        self.metrics: Dict[str, object] = {}
        self.group_counts: Dict[str, Counter] = defaultdict(Counter)
        self.generator: Optional[CandidateGenerator] = None

    def signature(self) -> str:
        h = hashlib.sha256()
        h.update(json.dumps([g.raw for g in self.groups], sort_keys=True).encode())
        h.update(str(len(self.store)).encode())
        for k in ("max_token_collision", "blocking_enabled", "blocking_max_block_size", "exhaustive",
                  "sorted_neighborhood_window", "case_insensitive", "treat_blank_as_null",
                  "relevance_constraint_mode", "relevance_nested_aggregation"):
            h.update(f"{k}={getattr(self.s, k)}".encode())
        return h.hexdigest()

    def run(self, checkpoint: Checkpoint, exhaustive: Optional[bool] = None) -> List[dict]:
        t0 = time.time()
        tracemalloc.start()
        gen = CandidateGenerator(self.store, self.groups, self.engine, self.s)
        pairs = gen.run(exhaustive=exhaustive)
        self.generator = gen
        self.metrics["candidate_generation_seconds"] = round(gen.stats.seconds, 1)
        self.metrics["candidate_sources"] = gen.source_counts()
        log.info("Candidate pairs: %s", self.metrics["candidate_sources"])

        done = checkpoint.evaluated_keys() if self.s.resume else set()
        recs = self.store.records
        todo: List[Tuple[int, int, dict]] = []
        skipped_same = 0
        for (i, j) in sorted(pairs):
            if recs[i].uri == recs[j].uri:
                skipped_same += 1
                continue
            if pair_key(recs[i].uri, recs[j].uri) in done:
                continue
            todo.append((i, j, pairs[(i, j)].as_dict()))
        self.metrics["pairs_already_evaluated_resume"] = len(done)
        self.metrics["pairs_evaluated"] = len(todo) + len(done)
        self.metrics["pairs_same_entity_skipped"] = skipped_same
        log.info("Pairs to evaluate: %d (resume skipped %d)", len(todo), len(done))

        prev = checkpoint.load_stats("group_counts") or {}
        for g, c in prev.items():
            self.group_counts[g].update(c)

        bs = max(1, self.s.batch_size)
        batches = [todo[k:k + bs] for k in range(0, len(todo), bs)]
        if self.s.workers > 1 and len(todo) > bs:
            ctx_args = dict(case_insensitive=self.engine.ctx.case_insensitive,
                            treat_blank_as_null=self.engine.ctx.treat_blank_as_null,
                            noise_dictionaries=self.engine.ctx.noise_dictionaries,
                            name_dictionary=self.engine.ctx.name_dictionary,
                            regexp_rules=self.engine.ctx.regexp_rules,
                            relevance_constraint_mode=self.engine.ctx.relevance_constraint_mode,
                            relevance_nested_aggregation=self.engine.ctx.relevance_nested_aggregation,
                            double_metaphone_max_length=self.engine.ctx.double_metaphone_max_length)
            with ProcessPoolExecutor(self.s.workers, initializer=_worker_init,
                                     initargs=([g.raw for g in self.groups], ctx_args, recs)) as pool:
                for n, (results, counts, snap) in enumerate(pool.map(_worker_batch, batches), 1):
                    self._absorb(checkpoint, batches[n - 1], results, counts)
                    LOG.merge_snapshot(snap)
                    log.info("Evaluated batch %d/%d", n, len(batches))
        else:
            _W.update(groups=self.groups, engine=self.engine, records=recs)
            for n, batch in enumerate(batches, 1):
                results, counts, _ = _worker_batch(batch)
                self._absorb(checkpoint, batch, results, counts)
                log.info("Evaluated batch %d/%d", n, len(batches))

        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.metrics["fn_seconds"] = round(time.time() - t0, 1)
        self.metrics["peak_memory_mb_python_heap"] = round(peak / 1e6, 1)
        rows = self.classify(checkpoint)
        return rows

    def _absorb(self, checkpoint, batch, results, counts) -> None:
        recs = self.store.records
        checkpoint.save_batch((pair_key(recs[i].uri, recs[j].uri) for i, j, _ in batch), results)
        for (g, k), v in counts.items():
            self.group_counts[g][k] += v
        checkpoint.save_stats("group_counts", {g: dict(c) for g, c in self.group_counts.items()})

    # ------------------------------------------------------------------
    def classify(self, checkpoint: Checkpoint) -> List[dict]:
        rows: List[dict] = []
        for key, g_uri, d in checkpoint.results():
            g = self.by_uri.get(g_uri)
            if g is None:
                continue
            rel_groups = self.report_pairs.get(key, set())
            in_report_group = g_uri.lower() in {x.lower() for x in rel_groups}
            st = self.steward.get(key)
            ver = self.verification.get(f"{key}##{g_uri}") or self.verification.get(key)
            cls, reason, detail = self._classify_one(d, rel_groups, st, ver)
            self.group_counts[g_uri]["found_in_reltio" if rel_groups else "python_only"] += 1
            if in_report_group:
                self.group_counts[g_uri]["found_in_reltio_same_group"] += 1
            row = {
                "PairKey": key,
                "EntityURI1": d["entity1"], "EntityURI2": d["entity2"],
                "EntityType": d["entity_type"],
                "MatchGroupURI": g_uri, "MatchGroupLabel": d["group_label"], "MatchGroupType": d["group_type"],
                "PythonRelevance": d["relevance"], "PythonAction": d["action"],
                "CandidateSource": d["source_label"],
                "TokenGroups": ", ".join(x.rsplit("/", 1)[-1] for x in d["source"]["token_groups"]),
                "OvercollidedGroups": ", ".join(x.rsplit("/", 1)[-1] for x in d["source"]["overcollided_groups"]),
                "BlockingPasses": d["blocking"],
                "InReltioOutput": "same group" if in_report_group else ("other group" if rel_groups else "no"),
                "ReltioGroupsForPair": ", ".join(sorted(x.rsplit("/", 1)[-1] for x in rel_groups)),
                "Classification": cls,
                "ReasonCode": reason,
                "ReasonDetail": detail,
                "RecommendedAction": NEXT_ACTION.get(reason, "Collect ground truth / verify on tenant."),
                "DecisiveStatus": d["decisive_status"],
                "ContributingAttributes": ", ".join(short(x) for x in d["contributing"]),
                "FailedAttributes": ", ".join(short(x) for x in d["failed"]),
                "DifferingAttributes": ", ".join(short(x) for x in d["differing"]),
                "NullContributions": ", ".join(d["nulls"]),
                "CrossInstanceNested": ", ".join(d["cross_instance"]),
                "PerAttributeResults": d["per_attribute"],
                "Logic": g.root.describe(),
                "StewardDecision": st.raw if st else "",
                "VerificationResult": json.dumps(ver)[:500] if ver else "",
            }
            row.update(d["values"])
            row["TraceText"] = d["trace_text"]
            row["TraceJSON"] = d["trace_json"]
            rows.append(row)
        return rows

    def _classify_one(self, d: dict, rel_groups: set, st: Optional[Decision], ver: Optional[dict]):
        if rel_groups:
            return RELTIO_MATCH, "", "Pair present in the Potential Match Report"
        if st is not None and st.kind == NOT_A_MATCH:
            return EXCLUDED, "", f"Steward decision {st.raw}"
        if st is not None and st.same_entity:
            return CONFIRMED_FN, self._mechanism(d), f"Steward decision {st.raw}; Reltio did not return the pair"
        if ver is not None:
            if ver.get("reltio_matched"):
                return RELTIO_MATCH, "", "Tenant verification says matched (report incomplete?)"
            if ver.get("reltio_matched") is False:
                mech = "TOKENIZATION_MISMATCH" if ver.get("common_tokens") == 0 else self._mechanism(d)
                return REVIEW, "NEEDS_STEWARD_REVIEW", f"Tenant verification: not matched; mechanism {mech}"
        mech = self._mechanism(d)
        if d["decisive_status"] == DOCUMENTED and mech in ("TOKENIZATION_MISMATCH", "CANDIDATE_GENERATION_MISS",
                                                           "RULE_LOGIC_MISMATCH"):
            return SUSPECTED_FN, mech, self._mech_detail(d, mech)
        return PY_CANDIDATE, mech, self._mech_detail(d, mech)

    def _mechanism(self, d: dict) -> str:
        label = d["source_label"]
        if label == "OVERCOLLIDED_TOKEN_ONLY":
            return "CANDIDATE_GENERATION_MISS"
        if label in ("BLOCKING_ONLY", "EXHAUSTIVE_ONLY"):
            return "TOKENIZATION_MISMATCH"
        if label == "TOKEN_OTHER_GROUP" and not self.s.global_candidate_pooling:
            return "TOKENIZATION_MISMATCH"
        if d["null_leaves"]:
            return "NULL_HANDLING_MISMATCH"
        if d["cleansed_leaves"]:
            return "CLEANSER_MISMATCH"
        if d["approx_leaves"] or d["permissive_leaves"]:
            return "COMPARATOR_BEHAVIOR_MISMATCH"
        return "RULE_LOGIC_MISMATCH"

    @staticmethod
    def _mech_detail(d: dict, mech: str) -> str:
        if mech == "TOKENIZATION_MISMATCH":
            return (f"No shared token phrase for this group in Python; found by {d['source_label']}"
                    + (f" ({d['blocking']})" if d["blocking"] else ""))
        if mech == "CANDIDATE_GENERATION_MISS":
            return "Shared phrase exists but is over-collided: " + ", ".join(d["source"]["overcollided_groups"])
        if mech == "NULL_HANDLING_MISMATCH":
            return "Match relies on null handling: " + ", ".join(d["nulls"])
        if mech == "CLEANSER_MISMATCH":
            return "Match relies on locally applied cleanser: " + ", ".join(short(x) for x in d["cleansed_leaves"])
        if mech == "COMPARATOR_BEHAVIOR_MISMATCH":
            return "Match relies on approximated/permissive comparator: " + ", ".join(
                short(x) for x in dict.fromkeys(d["approx_leaves"] + d["permissive_leaves"]))
        return "Token candidates shared and documented logic satisfied, yet Reltio did not return the pair"

