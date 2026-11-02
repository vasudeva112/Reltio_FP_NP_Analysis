"""
Component A - False positive analysis of the Potential Match Report.

A pair returned by Reltio is NOT treated as a false positive by default: Reltio only
established that the pair satisfies its configured logic. Classifications:
  Confirmed False Positive   steward NOT_A_MATCH
  Confirmed True Positive    steward MATCH / MERGED
  Suspected False Positive   risk score >= FP_SUSPECT_THRESHOLD
  Requires Steward Review    risk score >= FP_REVIEW_THRESHOLD
  Unclassified               otherwise
"""

import logging
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from .attribute_extractor import EntityRecord, record_from_report
from .candidate_generation import EntityStore
from .comparators.base import DOCUMENTED
from .exceptions_log import LOG
from .explain import per_attribute_results, risk_indicators, suggestions, value_columns
from .pair_deduplication import aggregate
from .report_reader import ReportRow
from .rule_config_parser import MatchGroup, attr_uri_from_dotted, short_attr
from .rule_evaluator import RuleEngine
from .steward_decisions import MERGED, MATCH, NOT_A_MATCH, Decision

log = logging.getLogger("fpnp")

CONFIRMED_FP = "Confirmed False Positive"
SUSPECTED_FP = "Suspected False Positive"
REVIEW = "Requires Steward Review"
CONFIRMED_TP = "Confirmed True Positive"
UNCLASSIFIED = "Unclassified"
FP_ORDER = [CONFIRMED_FP, CONFIRMED_TP, SUSPECTED_FP, REVIEW, UNCLASSIFIED]


def _frequency(rows: List[ReportRow], store: Optional[EntityStore], groups: List[MatchGroup],
               ctx, min_distinct_ratio: float = 0.3) -> Tuple[Dict[Tuple[str, str], int], Dict[str, float]]:
    """(dotted attribute, lower-cased value) -> number of distinct entities holding it, and per attribute
    the average number of entities per distinct value (its baseline frequency)."""
    seen: Dict[Tuple[str, str], set] = defaultdict(set)
    if store is not None and len(store):
        attrs = {a for g in groups for a in g.attributes()}
        for rec in store.records:
            for a in attrs:
                for v in rec.get(a).values:
                    seen[(short_attr(a), v.strip().lower())].add(rec.uri)
    else:
        for r in rows:
            for uri, vals in ((r.src_uri, r.src_values), (r.match_uri, r.match_values)):
                for a, text in vals.items():
                    for v in text.split(" | "):
                        if v.strip():
                            seen[(a, v.strip().lower())].add(uri)
    counts = {k: len(v) for k, v in seen.items()}
    per_attr: Dict[str, List[int]] = defaultdict(list)
    for (a, _), n in counts.items():
        per_attr[a].append(n)
    # average entities per distinct value; categorical attributes (few distinct values relative to
    # holders, e.g. flags, identifier types, common first names) get infinity so they are never flagged
    avg = {}
    for a, ns in per_attr.items():
        distinct_ratio = len(ns) / sum(ns)
        avg[a] = sum(ns) / len(ns) if distinct_ratio >= min_distinct_ratio else float("inf")
    return counts, avg


def _float(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


class FalsePositiveAnalyzer:
    def __init__(self, groups: List[MatchGroup], engine: RuleEngine, settings,
                 steward: Dict[str, Decision], store: Optional[EntityStore] = None, scorer=None):
        self.groups = groups
        self.by_uri = {g.uri.lower(): g for g in groups}
        self.engine = engine
        self.s = settings
        self.steward = steward
        self.store = store if store is not None and len(store) else None
        self.scorer = scorer          # optional fp_scores.ComparatorScorer fed with every evaluated row
        self.stats = Counter()

    def _record(self, uri: str, etype: str, crosswalks: str, values: Dict[str, str]) -> EntityRecord:
        if self.store is not None:
            rec = self.store.get(uri)
            if rec is not None:
                return rec
            self.stats["entity_missing_from_export"] += 1
            LOG.add("ENTITY_NOT_IN_EXPORT", "Report entity not found in the entity export - report values used",
                    example=uri)
        return record_from_report(uri, etype, crosswalks, values)

    def _evaluable_from_report(self, g: MatchGroup, columns: set) -> bool:
        return all(short_attr(a) in columns for a in g.attributes())

    def analyze(self, rows: List[ReportRow]) -> Tuple[List[dict], List[dict]]:
        t0 = time.time()
        freq, self.attr_avg = _frequency(rows, self.store, self.groups, self.engine.ctx,
                                         self.s.high_frequency_min_distinct_ratio)
        reltio_groups: Dict[str, set] = defaultdict(set)
        for r in rows:
            reltio_groups[r.key].add(r.rule_uri.lower())

        python_cache: Dict[str, List[str]] = {}
        out: List[dict] = []
        for n, r in enumerate(rows, 1):
            self.stats["report_rows"] += 1
            if r.src_uri == r.match_uri:
                self.stats["same_entity_rows"] += 1
                LOG.add("SAME_ENTITY_PAIR", "Report pair has the same entity on both sides - skipped",
                        example=r.src_uri)
                continue
            g = self.by_uri.get(r.rule_uri.lower())
            etype = r.entity_type
            L = self._record(r.src_uri, etype, r.src_crosswalks, r.src_values)
            R = self._record(r.match_uri, etype, r.match_crosswalks, r.match_values)
            if g is None:
                LOG.add("RULE_NOT_IN_CONFIG", "Report rule not found in the configuration export (or not "
                                              "selected) - pair not evaluated", group=r.rule_uri, severity="ERROR")
                out.append(self._unevaluated_row(r))
                continue
            missing_cols = [short_attr(a) for a in g.attributes() if short_attr(a) not in r.src_values]
            if missing_cols and L.origin == "report_values":
                LOG.add("ATTRIBUTE_PATH_MISMATCH", "Rule attribute has no column in the report - treated as unknown",
                        group=g.uri, attribute=", ".join(missing_cols), source=r.source_file)

            ev = self.engine.evaluate(g, L, R, cache=self.store is not None)
            if r.key not in python_cache:
                cols = set(r.src_values)
                python_cache[r.key] = sorted(
                    x.uri for x in self.groups
                    if x.entity_type == etype and (L.origin == "entity_export" or self._evaluable_from_report(x, cols))
                    and (x is g and ev.matched or x is not g and self.engine.evaluate(x, L, R).matched))
            row = self._row(r, g, ev, L, freq, reltio_groups[r.key], python_cache[r.key])
            out.append(row)
            if self.scorer is not None:
                self.scorer.score(r, g, ev, row)
            if n % 10000 == 0:
                log.info("FP analysis: %d / %d report rows", n, len(rows))
        self.stats["seconds"] = round(time.time() - t0, 1)
        return out, self.pair_level(out)

    def _unevaluated_row(self, r: ReportRow) -> dict:
        d = self._steward_decision(r.key)
        return {"PairKey": r.key, "OriginalOrder": f"{r.src_uri} -> {r.match_uri}", "SourceURI": r.src_uri,
                "MatchedURI": r.match_uri, "EntityType": r.entity_type, "MatchGroupURI": r.rule_uri,
                "MatchGroupLabel": r.rule_label, "Classification": self._steward_class(d) or UNCLASSIFIED,
                "ClassificationBasis": "steward decision" if d else "rule not in configuration",
                "Reason": "Match group not present in the configuration export; cannot explain the match.",
                "StewardDecision": d.raw if d else "", "SourceFile": r.source_file, "SourceRow": r.row_no}

    def _steward_decision(self, key: str) -> Optional[Decision]:
        d = self.steward.get(key)
        return d if d and d.kind in (NOT_A_MATCH, MATCH, MERGED) else None

    @staticmethod
    def _steward_class(d: Optional[Decision]) -> str:
        if d is None:
            return ""
        return CONFIRMED_FP if d.kind == NOT_A_MATCH else CONFIRMED_TP

    def _row(self, r: ReportRow, g: MatchGroup, ev, L: EntityRecord, freq, rel_groups: set,
             py_groups: List[str]) -> dict:
        reltio_rel = _float(r.extra.get("ReltioRelevance"))
        ind = risk_indicators(ev, g, freq, self.s.high_frequency_entities, self.s.low_margin, True,
                              self.attr_avg, self.s.high_frequency_factor, reltio_rel)
        score = round(sum(self.s.risk_weights.get(k, 0.0) for k, _ in ind), 2)
        d = self._steward_decision(r.key)
        cls = self._steward_class(d)
        if cls:
            basis = f"steward decision ({d.raw}, {d.source})"
        elif score >= self.s.fp_suspect_threshold:
            cls, basis = SUSPECTED_FP, f"risk score {score} >= {self.s.fp_suspect_threshold}"
        elif score >= self.s.fp_review_threshold:
            cls, basis = REVIEW, f"risk score {score} >= {self.s.fp_review_threshold}"
        else:
            cls, basis = UNCLASSIFIED, f"risk score {score}; no ground truth"
        self.stats[cls] += 1

        why = [f"Reltio returned the pair for {g.label}."]
        if ev.relevance is not None:
            why.append(f"Python relevance {ev.relevance:.4f}"
                       + (f" -> {ev.action_type} '{ev.action_label}'" if ev.action_type else " -> no threshold")
                       + ".")
        why.append(f"Python {'reproduces' if ev.matched else 'does NOT reproduce'} the match"
                   + (f" ({ev.not_applicable_reason})" if not ev.applicable else "") + ".")
        if ev.contributing():
            why.append("Contributing: " + ", ".join(ev.contributing()) + ".")
        if ev.failed():
            why.append("Failed: " + ", ".join(ev.failed()) + ".")
        if ev.null_contributions():
            why.append("Null-driven: " + ", ".join(ev.null_contributions()) + ".")
        if L.origin == "report_values":
            why.append("Values from the report (missing/null/empty indistinguishable).")
        if ind and not cls.startswith("Confirmed"):
            why.append("Investigate: " + "; ".join(f"{k}: {v}" for k, v in ind if k != "APPROXIMATION_DEPENDENT"))

        row = {
            "PairKey": r.key,
            "OriginalOrder": f"{r.src_uri} -> {r.match_uri}",
            "SourceURI": r.src_uri,
            "MatchedURI": r.match_uri,
            "EntityType": g.entity_type,
            "MatchGroupURI": g.uri,
            "MatchGroupLabel": g.label,
            "MatchGroupType": g.type,
            "ReltioGroupsForPair": ", ".join(sorted(x.rsplit("/", 1)[-1] for x in rel_groups)),
            "PythonMatchingGroups": ", ".join(x.rsplit("/", 1)[-1] for x in py_groups),
            "PythonMatched": ev.matched,
            "PythonRelevance": ev.relevance,
            "PythonAction": f"{ev.action_type}:{ev.action_label}" if ev.action_type else "",
            "ReltioRelevance": "" if reltio_rel is None else reltio_rel,
            "ReltioMatchAction": r.extra.get("ReltioMatchAction", ""),
            "Applicable": ev.applicable,
            "NotApplicableReason": ev.not_applicable_reason,
            "UnmetConstraintsIgnored": "; ".join(ev.ignored_constraints),
            "NegativeRuleHit": ev.negative_rule_hit,
            "ValueOrigin": L.origin,
            "ContributingAttributes": ", ".join(ev.contributing()),
            "DifferingAttributes": ", ".join(ev.differing()),
            "FailedAttributes": ", ".join(ev.failed()),
            "NullContributions": ", ".join(ev.null_contributions()),
            "CrossInstanceNested": ", ".join(ev.cross_instance_attrs),
            "Logic": g.root.describe(),
            "ComparatorClasses": g.summary()["ComparatorClasses"],
            "TokenClasses": g.summary()["TokenClasses"],
            "PerAttributeResults": per_attribute_results(ev),
            "RiskIndicators": ", ".join(k for k, _ in ind),
            "RiskDetails": " | ".join(f"{k}: {v}" for k, v in ind),
            "RiskScore": score,
            "Classification": cls,
            "ClassificationBasis": basis,
            "Reason": " ".join(why),
            "SuggestedRuleChanges": " ".join(suggestions(ind, ev, g)),
            "StewardDecision": d.raw if d else "",
            "DecisiveStatus": ev.decisive_status(),
            "SourceCrosswalks": r.src_crosswalks,
            "MatchedCrosswalks": r.match_crosswalks,
            "SourceFile": r.source_file,
            "SourceRow": r.row_no,
        }
        row.update(value_columns(ev, "src_", "match_"))
        row["TraceText"] = ev.trace_text()
        row["TraceJSON"] = ev.trace_json()
        return row

    @staticmethod
    def pair_level(rule_rows: List[dict]) -> List[dict]:
        out = []
        for key, rows in aggregate(rule_rows).items():
            cls = min((r["Classification"] for r in rows), key=FP_ORDER.index)
            first = rows[0]
            out.append({
                "PairKey": key,
                "OriginalOrder": first.get("OriginalOrder", ""),
                "EntityType": first.get("EntityType", ""),
                "RuleLevelRows": len(rows),
                "ReltioGroups": ", ".join(sorted({r.get("MatchGroupURI", "").rsplit("/", 1)[-1] for r in rows})),
                "PythonMatchingGroups": first.get("PythonMatchingGroups", ""),
                "MaxRiskScore": max((r.get("RiskScore") or 0) for r in rows),
                "RiskIndicators": ", ".join(sorted({i for r in rows for i in
                                                    (r.get("RiskIndicators") or "").split(", ") if i})),
                "Classification": cls,
                "StewardDecision": first.get("StewardDecision", ""),
            })
        return out
