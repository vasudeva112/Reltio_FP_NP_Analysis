"""
Comparator score view of the Potential Match Report (FP_Score_Report.xlsx).

For every report row the per-attribute outcome of the rule's comparators is classified
(Agree / Close variant / Phonetic or Fuzzy collision / Partial / Disagree / Null) with the
comparator detail (e.g. Double Metaphone codes, edit distance), the comparator relevance and
a Jaro-Winkler similarity of the compared values. A row is a comparator-flagged false
positive when an attribute evaluated with one of SCORE_OPERATORS has a result in
SCORE_TRIGGERS. Weights, thresholds and "what if this attribute fails" scores come from
the match group configuration through RuleEngine.what_if, so every rule is handled the
same way - nothing here names an attribute or a group.
"""

import itertools
import re
import statistics
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

import jellyfish

from .operators import BOTH_NULL
from .report_reader import ReportRow
from .rule_config_parser import CONSTRAINT_OPS, MatchGroup, Node, short_attr
from .rule_evaluator import GroupEvaluation, RuleEngine

AGREE, VARIANT, PHONETIC, FUZZY, PARTIAL, DISAGREE, NULL = (
    "Agree", "Close variant", "Phonetic collision", "Fuzzy collision", "Partial", "Disagree", "Null")
RESULTS = [AGREE, VARIANT, PHONETIC, FUZZY, PARTIAL, DISAGREE, NULL]
RESULT_MEANING = {
    AGREE: "Identical after normalisation.",
    VARIANT: "Different values the comparator accepted, and they are textually close (similarity at or "
             "above the cutoff).",
    PHONETIC: "Different values a phonetic comparator accepted although they are textually far apart.",
    FUZZY: "Different values a fuzzy comparator accepted although they are textually far apart.",
    PARTIAL: "Comparator did not accept the values, but they are close (comparator relevance at or above the "
             "cutoff).",
    DISAGREE: "Comparator did not accept the values.",
    NULL: "Value missing on one or both records.",
}
_PHONETIC_HINT = ("metaphone", "soundex", "phonetic")
_MAX_VALUES = 5


def _values(display: str) -> List[str]:
    """Values of a LeafResult.left/right display string ('a | raw=>cleansed', '<MISSING>' for none)."""
    out = []
    for part in (display or "").replace(" ; ", " | ").split(" | "):
        part = part.split("=>")[-1].strip()
        if part and not (part.startswith("<") and part.endswith(">")):
            out.append(part)
    return out[:_MAX_VALUES]


def similarity(left: Sequence[str], right: Sequence[str], norm=str.lower) -> Optional[float]:
    """Best Jaro-Winkler similarity across the value pairs (independent of the rule's comparator)."""
    best = None
    for a in left:
        for b in right:
            s = jellyfish.jaro_winkler_similarity(norm(a), norm(b))
            best = s if best is None else max(best, s)
    return best


def classify_leaf(lf, norm, cutoff: float, relevance_mode: bool) -> Tuple[str, Optional[float]]:
    """(result, similarity) for one attribute operand."""
    if lf.null_case:
        return NULL, None
    pair = lf.matched_values if any(lf.matched_values) else None
    sim = similarity([pair[0]], [pair[1]], norm) if pair else similarity(_values(lf.left), _values(lf.right), norm)
    if lf.passed:
        if (norm(pair[0]) == norm(pair[1])) if pair else (sim is None or sim >= 0.9999):
            return AGREE, sim
        if sim is not None and sim >= cutoff:
            return VARIANT, sim
        return (PHONETIC if any(h in (lf.comparator or "").lower() for h in _PHONETIC_HINT) else FUZZY), sim
    if relevance_mode and lf.relevance >= cutoff:
        return PARTIAL, sim
    return DISAGREE, sim


def scored_nodes(g: MatchGroup) -> List[Node]:
    """Attribute operands of the rule that count towards a match (not under NOT, not constraints)."""
    out: List[Node] = []

    def walk(n: Node, under_not: bool) -> None:
        if n.kind == "op":
            if not under_not and n.op not in CONSTRAINT_OPS:
                out.append(n)
            return
        for c in n.children:
            walk(c, under_not or n.kind == "not")
    walk(g.root, False)
    return out


def _rule(g: MatchGroup) -> str:
    return g.uri.rsplit("/", 1)[-1]


def _r(v: Optional[float], n: int = 4):
    return None if v is None else round(v, n)


class ComparatorScorer:
    def __init__(self, groups: List[MatchGroup], engine: RuleEngine, settings):
        self.engine = engine
        self.norm = engine.ctx.norm
        self.s = settings
        self.ops = set(settings.score_operators)
        self.triggers = set(settings.score_triggers)
        self.cutoff = settings.score_similarity_cutoff
        self.segment_label = settings.segment_label or "Segment"
        self.segment_re = re.compile(settings.segment_crosswalk_regex) if settings.segment_crosswalk_regex else None
        self.groups = groups
        self.nodes = {g.uri: scored_nodes(g) for g in groups}
        self.lowest = {g.uri: min((t.lo for t in g.thresholds), default=None) for g in groups}
        # attributes shown as column blocks in the detail sheet, in configuration order
        self.score_attrs: List[str] = list(dict.fromkeys(
            short_attr(n.attribute) for g in groups for n in self.nodes[g.uri] if n.op in self.ops))

        self.detail: List[dict] = []
        self.attribute_rows: List[dict] = []
        self.rules_seen: List[str] = []
        self.count: Dict[Tuple, Counter] = defaultdict(Counter)       # metric key -> rule -> rows
        self.pairs: Dict[Tuple, Set[str]] = defaultdict(set)           # metric key -> pair keys (all rules)
        self.rule_pairs: Dict[str, Set[str]] = defaultdict(set)
        self.attr_in_rule: Dict[str, Set[str]] = defaultdict(set)      # attribute -> rules using it
        self.trigger_rules: Dict[str, Set[str]] = defaultdict(set)    # attribute -> rules where it can flag
        for g in groups:
            for n in self.nodes[g.uri]:
                if n.op in self.ops:
                    self.trigger_rules[short_attr(n.attribute)].add(_rule(g))
        self.relevance: Dict[str, List[float]] = defaultdict(list)
        self.flagged_relevance: Dict[str, List[float]] = defaultdict(list)
        self.reltio_diff: Dict[str, List[float]] = defaultdict(list)
        self._first_pair: Set[str] = set()

    # ------------------------------------------------------------------
    def segment(self, r: ReportRow) -> Optional[bool]:
        if self.segment_re is None:
            return None
        def hit(cw: str) -> bool:
            return any(self.segment_re.search(x.strip()) for x in re.split(r"\s*[|;,]\s*", cw or "") if x.strip())
        return hit(r.src_crosswalks) and hit(r.match_crosswalks)

    def _add(self, key: Tuple, rule: str, pair: str) -> None:
        self.count[key][rule] += 1
        self.pairs[key].add(pair)

    def score(self, r: ReportRow, g: MatchGroup, ev: GroupEvaluation, fp_row: dict) -> None:
        rule, pair = _rule(g), r.key
        if rule not in self.rules_seen:
            self.rules_seen.append(rule)
        seg = self.segment(r)
        self.rule_pairs[rule].add(pair)
        self._add(("rows",), rule, pair)
        if seg:
            self._add(("segment",), rule, pair)
        if ev.relevance is not None:
            self.relevance[rule].append(ev.relevance)
            self._add(("band", ev.action_label or "(below every threshold)"), rule, pair)
            lo = self.lowest[g.uri]
            if lo is not None and ev.relevance < lo - 1e-9:
                self._add(("below",), rule, pair)
            elif lo is not None and ev.relevance - lo < self.s.low_margin:
                self._add(("margin",), rule, pair)
            rr = fp_row.get("ReltioRelevance")
            if rr not in (None, ""):
                self.reltio_diff[rule].append(abs(float(rr) - ev.relevance))

        leaves = {lf.attribute: lf for lf in ev.attribute_leaves()}
        rel_mode = ev.relevance is not None
        actual = {a: lf.relevance for a, lf in leaves.items()}
        base = self.engine.what_if(g, actual)[1] if rel_mode else None
        blocks: Dict[str, dict] = {}
        triggered: List[Tuple[str, str, object, float]] = []
        for node in self.nodes[g.uri]:
            lf = leaves.get(node.attribute)
            if lf is None:
                continue
            a = short_attr(node.attribute)
            self.attr_in_rule[a].add(rule)
            result, sim = classify_leaf(lf, self.norm, self.cutoff, rel_mode)
            is_trigger = node.op in self.ops and result in self.triggers
            self._add(("attr", a, result), rule, pair)
            lost = None
            if rel_mode and result != AGREE and actual.get(node.attribute, 1.0) < 1.0:
                up = dict(actual)
                up[node.attribute] = 1.0
                lost = round(self.engine.what_if(g, up)[1] - base, 4)
            if is_trigger:
                triggered.append((a, result, lf, lost))
            if result != AGREE:
                self.attribute_rows.append({
                    "PairKey": pair, "Rule": rule, "Attribute": a, "Operator": node.op,
                    "Comparator": lf.comparator, "Src Value": lf.left, "Match Value": lf.right,
                    "Comparator Detail": lf.detail or lf.null_case, "Comparator Rel": _r(lf.relevance),
                    "Similarity": _r(sim), "Result": result, "Flags Pair": "Y" if is_trigger else "N",
                    "Score Lost": lost, "Relevance Score": _r(ev.relevance), self.segment_label: _yn(seg)})
            if a in self.score_attrs and a not in blocks:
                blocks[a] = {f"{a} (Src)": lf.left, f"{a} (Match)": lf.right, f"{a} Operator": node.op,
                             f"{a} Comparator": lf.comparator,
                             f"{a} Detail": lf.detail if lf.null_case != BOTH_NULL else lf.null_case,
                             f"{a} Rel": _r(lf.relevance) if rel_mode else ("1" if lf.passed else "0"),
                             f"{a} Similarity": _r(sim), f"{a} Result": result}

        if not triggered:
            return
        self._add(("flagged",), rule, pair)
        if seg:
            self._add(("flagged_segment",), rule, pair)
        self._add(("flagged_class", fp_row.get("Classification", "")), rule, pair)
        if rel_mode:
            self.flagged_relevance[rule].append(ev.relevance)
        lo = self.lowest[g.uri]
        row = {"PairKey": pair, "Rule": rule, "Match Group": g.label, "Source Entity": r.src_uri,
               "Match Entity": r.match_uri, self.segment_label: _yn(seg)}
        for a in self.score_attrs:
            row.update(blocks.get(a) or {f"{a} Result": "N/A"})
        row.update({
            "Relevance Score": _r(ev.relevance),
            "Reltio Relevance": fp_row.get("ReltioRelevance", ""),
            "Threshold": lo,
            "Margin Above Threshold": _r(ev.relevance - lo) if rel_mode and lo is not None else None,
            "Band": ev.action_label or ("(below every threshold)" if rel_mode else ""),
            "FP Triggers": ", ".join(f"{a}: {res}" for a, res, _, _ in triggered),
            "FP Reason": "; ".join(f"{a} {res.lower()} ({lf.left} vs {lf.right}"
                                   + (f", {lf.detail}" if lf.detail else "") + ")" for a, res, lf, _ in triggered),
            "Score Impact": "; ".join(f"{a} -{lost:.3f}" for a, _, _, lost in triggered if lost),
            "Risk Classification": fp_row.get("Classification", ""),
            "Risk Score": fp_row.get("RiskScore", ""),
            "First Row For Pair": "Y" if pair not in self._first_pair else "N",
        })
        self._first_pair.add(pair)
        self.detail.append(row)

    # ------------------------------------------------------------------
    def summary_rows(self) -> List[dict]:
        rules = self.rules_seen
        out: List[dict] = []

        def emit(section: str, metric: str, key: Optional[Tuple], values: Optional[Dict[str, object]] = None,
                 total=None, unique=None, note: str = "", attribute: str = "", result: str = "",
                 trigger: str = "") -> None:
            row = {"Section": section, "Metric": metric}
            for ru in rules:
                row[ru] = values.get(ru, "") if values is not None else self.count[key].get(ru, 0)
            row["Total"] = total if values is not None else sum(self.count[key].values())
            row["Unique Pairs"] = unique if unique is not None else (len(self.pairs[key]) if key else "")
            row.update({"Attribute": attribute, "Result": result, "Flags Pair": trigger, "Note": note})
            out.append(row)

        rows_total = sum(self.count[("rows",)].values())
        emit("Pair counts", "Report rows (pair + rule)", ("rows",))
        emit("Pair counts", "Unique pairs", None, {ru: len(self.rule_pairs[ru]) for ru in rules},
             total=sum(len(self.rule_pairs[ru]) for ru in rules), unique=len(self.pairs[("rows",)]),
             note="a pair returned by several rules is counted under each rule")
        if self.segment_re is not None:
            seg_label = self.segment_label
            emit("Pair counts", f"{seg_label} rows", ("segment",),
                 note=f"both entities have a crosswalk matching {self.s.segment_crosswalk_regex}")
            emit("Pair counts", f"{seg_label} % of rows", None,
                 {ru: _pct(self.count[("segment",)][ru], self.count[("rows",)][ru]) for ru in rules},
                 total=_pct(sum(self.count[("segment",)].values()), rows_total), unique="")
            emit("Pair counts", f"Non-{seg_label} rows", None,
                 {ru: self.count[("rows",)][ru] - self.count[("segment",)][ru] for ru in rules},
                 total=rows_total - sum(self.count[("segment",)].values()), unique="")

        for a in dict.fromkeys(a for (_, a, _) in (k for k in self.count if k[0] == "attr")):
            for res in RESULTS:
                key = ("attr", a, res)
                if not self.count[key] and res not in (DISAGREE, NULL):
                    continue
                vals = {ru: (self.count[key].get(ru, 0) if ru in self.attr_in_rule[a] else "-") for ru in rules}
                trig = ""
                if res in self.triggers and self.trigger_rules[a]:
                    used = self.attr_in_rule[a] & set(rules)
                    trig = "Y" if used <= self.trigger_rules[a] else ", ".join(
                        ru for ru in rules if ru in self.trigger_rules[a])
                emit("Comparator results", f"{a} - {res}", key, vals, total=sum(self.count[key].values()),
                     unique=len(self.pairs[key]), attribute=a, result=res, trigger=trig,
                     note=RESULT_MEANING[res] + (" Flags the pair" + ("." if trig == "Y" else f" in {trig}.")
                                                if trig else ""))

        flagged = sum(self.count[("flagged",)].values())
        emit("False positives", "Comparator-flagged rows", ("flagged",),
             note=f"an attribute evaluated with {', '.join(sorted(self.ops))} has a result in: "
                  f"{', '.join(sorted(self.triggers))}")
        if self.segment_re is not None:
            emit("False positives", f"  of which {self.segment_label}", ("flagged_segment",))
        emit("False positives", "FP rate (% of rows)", None,
             {ru: _pct(self.count[("flagged",)][ru], self.count[("rows",)][ru]) for ru in rules},
             total=_pct(flagged, rows_total), unique="")
        for cls in sorted({k[1] for k in self.count if k[0] == "flagged_class"}):
            emit("False positives", f"  risk classification: {cls}", ("flagged_class", cls),
                 note="classification from the risk-indicator analysis (FPNP_Analysis.xlsx)")

        if any(self.relevance.values()):
            allv = [v for vs in self.relevance.values() for v in vs]
            for label, fn in (("Average", statistics.fmean), ("Minimum", min), ("Median", statistics.median),
                              ("Maximum", max)):
                emit("Relevance", f"{label} relevance score", None,
                     {ru: _r(fn(self.relevance[ru])) if self.relevance[ru] else "" for ru in rules},
                     total=_r(fn(allv)), unique="")
            fl = [v for vs in self.flagged_relevance.values() for v in vs]
            emit("Relevance", "Average relevance of flagged rows", None,
                 {ru: _r(statistics.fmean(self.flagged_relevance[ru])) if self.flagged_relevance[ru] else ""
                  for ru in rules}, total=_r(statistics.fmean(fl)) if fl else "", unique="")
            emit("Relevance", "Lowest threshold", None,
                 {_rule(g): self.lowest[g.uri] for g in self.groups if _rule(g) in rules and self.lowest[g.uri]
                  is not None}, total="", unique="")
            emit("Relevance", "Rows scored below the lowest threshold", ("below",),
                 note="Python relevance below every actionThreshold although Reltio returned the pair")
            emit("Relevance", f"Rows within {self.s.low_margin} above the lowest threshold", ("margin",))
            for band in sorted({k[1] for k in self.count if k[0] == "band"}):
                emit("Bands", band, ("band", band), note="actionThreshold label of the Python relevance score")
        if any(self.reltio_diff.values()):
            emit("Reltio relevance", "Rows with Reltio relevance", None,
                 {ru: len(self.reltio_diff[ru]) for ru in rules},
                 total=sum(len(v) for v in self.reltio_diff.values()), unique="")
            emit("Reltio relevance", "Rows where Python differs by more than 0.005", None,
                 {ru: sum(1 for d in self.reltio_diff[ru] if d > 0.005) for ru in rules},
                 total=sum(1 for v in self.reltio_diff.values() for d in v if d > 0.005), unique="")
        return out

    # ------------------------------------------------------------------
    def weight_rows(self) -> List[dict]:
        out = []
        for g in self.groups:
            rel_mode = g.is_relevance
            nodes = self.nodes[g.uri]
            total = sum(g.weight_for(n.attribute) for n in nodes) if rel_mode else None
            for n in nodes:
                passed, score = self.engine.what_if(g, failing=[n.attribute])
                tok = g.token_classes.get(n.attribute)
                w = g.weight_for(n.attribute)
                out.append({
                    "Rule": _rule(g), "Rule Type": g.type, "Attribute": short_attr(n.attribute), "Operator": n.op,
                    "Comparator": self.engine.comparator(g, n.attribute).RELTIO_NAME if n.op != "multi"
                    else "CrossMultiComparator",
                    "Token Class": tok.short if tok else "",
                    "Config Weight": g.weights.get(n.attribute, "") if rel_mode else "",
                    "Weight Used": w if rel_mode else "",
                    "Weight Share": _r(w / total) if rel_mode and total else "",
                    "Score If Only This Fails": _r(score) if rel_mode else "",
                    "Score Lost": _r(1 - score) if rel_mode else "",
                    "Still Matches If Only This Fails": "Yes" if passed else "No",
                    "Flags Pair When": ", ".join(sorted(self.triggers)) if n.op in self.ops else "",
                })
            for n in g.root.walk():
                if n.kind == "op" and n.op in CONSTRAINT_OPS:
                    out.append({"Rule": _rule(g), "Rule Type": g.type, "Attribute": short_attr(n.attribute),
                                "Operator": f"{n.op} {n.values}", "Comparator": "(filter)",
                                "Weight Used": n.params.get("weight", 0) if rel_mode else "",
                                "Still Matches If Only This Fails": "filter - not scored"})
        return out

    def tolerance_rows(self) -> List[dict]:
        out = []
        for g in self.groups:
            names = [n.attribute for n in self.nodes[g.uri]]
            alone = [short_attr(a) for a in names if self.engine.what_if(g, failing=[a])[0]]
            together = []
            for a, b in itertools.combinations(names, 2):
                ok, score = self.engine.what_if(g, failing=[a, b])
                if ok:
                    together.append(f"{short_attr(a)} + {short_attr(b)}"
                                    + (f" ({score:.3f})" if g.is_relevance else ""))
            lo = self.lowest[g.uri]
            out.append({
                "Rule": _rule(g), "Rule Type": g.type, "Logic": g.root.describe(),
                "Total Weight": round(sum(g.weight_for(a) for a in names), 4) if g.is_relevance else "",
                "Bands": "; ".join(f"{t.label or t.type}: {t.lo}-{t.hi}" for t in g.thresholds),
                "Lowest Threshold": lo if lo is not None else "",
                "Score Headroom": _r(1 - lo) if lo is not None else "",
                "Attributes That Can Fail Alone": ", ".join(alone) or "none",
                "Attribute Pairs That Can Fail Together": "; ".join(together) or "none",
                "Pairs Count": len(together),
            })
        return out


def _yn(v: Optional[bool]) -> str:
    return "" if v is None else ("Y" if v else "N")


def _pct(part: int, whole: int) -> object:
    return round(100.0 * part / whole, 1) if whole else ""
