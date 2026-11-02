"""
Client-facing HTML summary of a run (FPNP_Report.html next to the workbook).

One self-contained file: inline CSS, HTML/SVG charts and a few lines of JavaScript for
tooltips - no network access needed, so it can be e-mailed or printed to PDF. The
workbook remains the detailed deliverable; this page explains the headline numbers.

Built either from the in-memory results of pipeline.run() or, for an existing run,
from the files in OUTPUT_DIR (fpnp_analyzer.py html).
"""

import csv
import html
import json
import os
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from .false_negative_analyzer import (CONFIRMED_FN, EXCLUDED, PY_CANDIDATE, RELTIO_MATCH, REVIEW as FN_REVIEW,
                                      SUSPECTED_FN)
from .fp_scores import RESULT_MEANING
from .false_positive_analyzer import (CONFIRMED_FP, CONFIRMED_TP, REVIEW as FP_REVIEW, SUSPECTED_FP, UNCLASSIFIED)
from .legacy import load_properties

# classification -> (css colour token, short label, plain-language meaning)
FP_CLASSES = [
    (CONFIRMED_FP, "critical", "Confirmed false positive", "A data steward marked the pair as not a match."),
    (SUSPECTED_FP, "serious", "Suspected false positive", "Several strong warning signs; likely not the same person."),
    (FP_REVIEW, "warning", "Needs steward review", "Some warning signs; a person should decide."),
    (UNCLASSIFIED, "neutral", "No concerns found", "No significant warning signs; not proof of a correct match."),
    (CONFIRMED_TP, "good", "Confirmed true positive", "A data steward confirmed the pair is a real duplicate."),
]
FN_CLASSES = [
    (CONFIRMED_FN, "critical", "Confirmed missed match", "Steward-confirmed duplicate that Reltio did not return."),
    (SUSPECTED_FN, "serious", "Suspected missed match", "Satisfies the documented rule logic but Reltio did not return it."),
    (PY_CANDIDATE, "candidate", "Possible missed match", "Depends on approximated behaviour; lower confidence."),
    (FN_REVIEW, "warning", "Needs review", "Tenant verification disagreed or was inconclusive."),
    (EXCLUDED, "neutral", "Excluded", "Steward or negative-rule evidence says not a match."),
    (RELTIO_MATCH, "good", "Already found by Reltio", "Pair is already in the Potential Match Report."),
]

# risk indicator -> (plain-language label, explanation); informational indicators are left out of charts
SIGNALS = {
    "CONSTRAINT_VIOLATED": ("Rule's own condition not met",
                            "Reltio returned the pair although a filter in the rule (e.g. identifier type must be "
                            "SSN/ITN/EIN) is not satisfied."),
    "FAILED_OPERAND_STILL_MATCHED": ("Matched despite a disagreeing attribute",
                                     "At least one compared attribute did not match, but the other attributes "
                                     "outweighed it."),
    "POPULATED_VALUES_DIFFER": ("Both values present but different",
                                "Both records hold a value for an attribute and the values differ."),
    "ONE_SIDED_NULL": ("Value missing on one record",
                       "An attribute counted as matching because one record has no value."),
    "NULL_CONTRIBUTION": ("Missing values counted as a match",
                          "Empty values were treated as agreeing (exactOrNull / exactOrAllNull)."),
    "PERMISSIVE_COMPARATOR": ("Fuzzy comparison accepted different values",
                              "A phonetic or fuzzy comparator treated two different values as equal."),
    "CROSS_INSTANCE_NESTED_MATCH": ("Nested values matched across entries",
                                    "e.g. the identifier type matched on one identifier and the value on another."),
    "MULTI_VALUE_PARTIAL": ("Only some of several values matched",
                            "A multi-valued attribute matched on one value while others differ."),
    "LOW_MARGIN": ("Score just above the threshold",
                   "The match score is within a few points of the lowest match threshold."),
    "HIGH_FREQUENCY_VALUE": ("Matched on a widely shared value",
                             "The matching value is held by many records - possibly a placeholder."),
    "PYTHON_DISAGREES": ("Independent re-evaluation disagrees",
                         "The independent re-evaluation of the rule did not reproduce Reltio's result."),
}
INFORMATIONAL = {"APPROXIMATION_DEPENDENT", "RELTIO_RELEVANCE_DIFFERS"}

FN_REASONS = {
    "NULL_HANDLING": "Missing values handled differently",
    "TOKENIZATION": "Records never compared (no shared match token)",
    "BLOCKING_ONLY": "Found only by extra blocking keys",
    "OVERCOLLISION": "Match token shared by too many records",
    "CROSS_INSTANCE": "Nested values matched across entries",
    "NEGATIVE_RULE": "Blocked by a negative rule",
}

FP_COLUMNS = ["PairKey", "SourceURI", "MatchedURI", "EntityType", "MatchGroupURI", "MatchGroupLabel",
              "MatchGroupType", "PythonMatched", "PythonRelevance", "RiskIndicators", "RiskScore", "Classification",
              "FailedAttributes", "DifferingAttributes", "UnmetConstraintsIgnored"]
FN_COLUMNS = ["PairKey", "EntityType", "MatchGroupURI", "MatchGroupLabel", "Classification", "ReasonCode",
              "PythonRelevance"]


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------
def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _f(v) -> Optional[float]:
    try:
        return None if v in (None, "", "None", "nan") else float(v)
    except (TypeError, ValueError):
        return None


def _b(v) -> bool:
    return v is True or str(v).strip().lower() == "true"


def _n(v) -> str:
    return f"{v:,}" if isinstance(v, int) else _e(v)


def _pct(part: int, whole: int, digits: int = 1) -> str:
    return f"{part / whole:.{digits}%}" if whole else "-"


def _rule(uri_or_label: str) -> str:
    return (uri_or_label or "").rsplit("/", 1)[-1]


def _split(v: str) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _tip(text: str) -> str:
    return f' tabindex="0" data-tip="{_e(text)}"'


def _table(headers: Sequence[str], rows: Sequence[Sequence], num_cols: Sequence[int] = (), cls: str = "") -> str:
    th = "".join(f'<th{" class=num" if i in num_cols else ""}>{_e(h)}</th>' for i, h in enumerate(headers))
    body = []
    for r in rows:
        body.append("<tr>" + "".join(f'<td{" class=num" if i in num_cols else ""}>{c}</td>'
                                     for i, c in enumerate(r)) + "</tr>")
    return f'<div class="tablewrap {cls}"><table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _data_table(headers, rows, num_cols=()) -> str:
    return f'<details class="dataview"><summary>View as table</summary>{_table(headers, rows, num_cols)}</details>'


# ----------------------------------------------------------------------
# chart components (HTML/CSS bars, SVG histogram)
# ----------------------------------------------------------------------
def _part_to_whole(counts: Counter, classes, unit: str = "pairs") -> str:
    total = sum(counts.values())
    segs, legend = [], []
    for key, tone, label, meaning in classes:
        n = counts.get(key, 0)
        if n:
            segs.append(f'<span class="seg t-{tone}" style="flex:{n} 1 0"'
                        f'{_tip(f"{label}: {n:,} {unit} ({_pct(n, total)})")}></span>')
        legend.append(f'<li><span class="sw t-{tone}"></span><span class="lg-label">{_e(label)}</span>'
                      f'<span class="lg-val">{n:,}</span><span class="lg-pct">{_pct(n, total)}</span>'
                      f'<span class="lg-note">{_e(meaning)}</span></li>')
    return f'<div class="stack">{"".join(segs)}</div><ul class="legend">{"".join(legend)}</ul>'


def _stacked_rows(rows: List[Tuple[str, Counter]], classes, unit: str = "rows", normalize: bool = False) -> str:
    """One bar per row; normalize=True draws each bar at full width (shares within the row)."""
    mx = max((sum(c.values()) for _, c in rows), default=0) or 1
    out = []
    for label, c in rows:
        total = sum(c.values())
        segs = []
        for key, tone, name, _ in classes:
            n = c.get(key, 0)
            if n:
                segs.append(f'<span class="seg t-{tone}" style="flex:{n} 1 0"'
                            f'{_tip(f"{label} - {name}: {n:,} {unit} ({_pct(n, total)})")}></span>')
        out.append(f'<div class="srow"><div class="srow-label">{_e(label)}</div>'
                   f'<div class="srow-track"><div class="stack thin" style="width:{100 if normalize else max(total / mx * 100, 0.6):.2f}%">'
                   f'{"".join(segs)}</div></div><div class="srow-val">{total:,}</div></div>')
    legend = "".join(f'<li><span class="sw t-{tone}"></span><span class="lg-label">{_e(name)}</span></li>'
                     for key, tone, name, _ in classes if any(c.get(key) for _, c in rows))
    table = _data_table(["Match rule"] + [name for _, _, name, _ in classes] + ["Total"],
                        [[_e(label)] + [f"{c.get(k, 0):,}" for k, *_ in classes] + [f"{sum(c.values()):,}"]
                         for label, c in rows], num_cols=range(1, len(classes) + 2))
    return f'<ul class="legend inline">{legend}</ul><div class="srows">{"".join(out)}</div>{table}'


def _bar_list(items: List[Tuple[str, int, str]], total: int = 0, unit: str = "pairs") -> str:
    """items: (label, value, tooltip detail). One series -> one colour."""
    if not items:
        return '<p class="empty">Nothing to show.</p>'
    mx = max(v for _, v, _ in items) or 1
    out = []
    for label, v, detail in items:
        share = f" ({_pct(v, total)} of {unit})" if total else ""
        out.append(f'<div class="brow"><div class="brow-label">{_e(label)}</div>'
                   f'<div class="brow-track"><span class="bar" style="width:{max(v / mx * 100, 0.6):.2f}%"'
                   f'{_tip(f"{label}: {v:,} {unit}{share}" + (f" - {detail}" if detail else ""))}></span></div>'
                   f'<div class="brow-val">{v:,}{f"<small>{_pct(v, total, 0)}</small>" if total else ""}</div></div>')
    table = _data_table(["Item", unit.capitalize()] + (["Share"] if total else []),
                        [[_e(l), f"{v:,}"] + ([_pct(v, total)] if total else []) for l, v, _ in items],
                        num_cols=(1, 2))
    return f'<div class="brows">{"".join(out)}</div>{table}'


def _histogram(values: List[float], threshold: Optional[float], title: str) -> str:
    if not values:
        return ""
    step = 0.02
    lo = min(min(values), threshold if threshold is not None else 1.0)
    lo = max(0.0, int(lo / step) * step)
    nbins = max(1, round((1.0 - lo) / step) + 1)
    bins = [0] * nbins
    for v in values:
        bins[min(nbins - 1, max(0, int(round((v - lo) / step, 6))))] += 1
    W, H, padl, padb, padt = 560, 170, 20, 26, 14
    bw = (W - padl * 2) / nbins
    mx = max(bins) or 1
    parts = [f'<line class="axis" x1="{padl}" y1="{H - padb}" x2="{W - padl}" y2="{H - padb}"/>']
    for i, n in enumerate(bins):
        if not n:
            continue
        h = max((H - padb - padt) * n / mx, 2)
        x = padl + i * bw + 1
        a, b = lo + i * step, lo + (i + 1) * step
        parts.append(f'<rect class="hbar" x="{x:.1f}" y="{H - padb - h:.1f}" width="{max(bw - 2, 1):.1f}" '
                     f'height="{h:.1f}" rx="2"{_tip(f"Score {a:.2f}-{b:.2f}: {n:,} rows")}/>')
    for i in range(0, nbins, max(1, nbins // 6)):
        x = padl + i * bw
        parts.append(f'<text class="tick" x="{x:.1f}" y="{H - 8}">{lo + i * step:.2f}</text>')
    if threshold is not None and lo <= threshold <= 1.0:
        x = padl + (threshold - lo) / step * bw
        left = x > W * 0.55
        parts.append(f'<line class="thr" x1="{x:.1f}" y1="{padt - 6}" x2="{x:.1f}" y2="{H - padb}"/>'
                     f'<text class="thr-label" x="{x - 5 if left else x + 5:.1f}" y="{padt - 2}" '
                     f'text-anchor="{"end" if left else "start"}">lowest threshold {threshold:g}</text>')
    table = _data_table(["Score band", "Rows"],
                        [[f"{lo + i * step:.2f}-{lo + (i + 1) * step:.2f}", f"{n:,}"] for i, n in enumerate(bins) if n],
                        num_cols=(1,))
    return (f'<figure class="hist"><figcaption>{_e(title)}</figcaption>'
            f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{_e(title)} score distribution">{"".join(parts)}</svg>'
            f'{table}</figure>')


def _tile(label: str, value: str, note: str = "", tone: str = "") -> str:
    return (f'<div class="tile{" tone-" + tone if tone else ""}"><div class="tile-label">{_e(label)}</div>'
            f'<div class="tile-value">{value}</div><div class="tile-note">{_e(note)}</div></div>')


# ----------------------------------------------------------------------
# loading an existing run from disk
# ----------------------------------------------------------------------
def _read_csv(path: str, columns: Optional[Sequence[str]] = None) -> List[dict]:
    if not os.path.isfile(path):
        return []
    csv.field_size_limit(2 ** 31 - 1)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        if columns is None:
            return list(rd)
        keep = [c for c in columns if c in (rd.fieldnames or [])]
        return [{c: r.get(c, "") for c in keep} for r in rd]


def _read_detail(det: str, name: str, columns: Sequence[str]) -> List[dict]:
    pq = os.path.join(det, f"{name}.parquet")
    if os.path.isfile(pq):
        try:
            import pyarrow.parquet as papq
            have = set(papq.read_schema(pq).names)
            tbl = papq.read_table(pq, columns=[c for c in columns if c in have])
            return tbl.to_pylist()
        except Exception:      # fall back to the CSV copy
            pass
    return _read_csv(os.path.join(det, f"{name}.csv"), columns)


def load_outputs(out_dir: str) -> dict:
    det = os.path.join(out_dir, "detail")
    meta = {}
    mpath = os.path.join(out_dir, "run_metadata.json")
    if os.path.isfile(mpath):
        with open(mpath, encoding="utf-8") as fh:
            meta = json.load(fh)
    return {
        "fp_rows": _read_detail(det, "potential_match_analysis", FP_COLUMNS),
        "fp_pairs": _read_csv(os.path.join(det, "potential_match_pairs.csv")),
        "fn_rows": _read_detail(det, "false_negative_candidates", FN_COLUMNS),
        "rule_rows": _read_csv(os.path.join(det, "rule_level_summary.csv")),
        "recall_rows": _read_csv(os.path.join(det, "candidate_recall.csv")),
        "exceptions": _read_csv(os.path.join(det, "processing_exceptions.csv")),
        "meta": meta,
        "scores": load_scores(det),
    }


def load_scores(det: str) -> Optional[dict]:
    summary = _read_csv(os.path.join(det, "fp_score_summary.csv"))
    if not summary:
        return None
    seg = next((r["Metric"][:-5] for r in summary if r.get("Section") == "Pair counts"
                and r.get("Metric", "").endswith(" rows") and r["Metric"] not in ("Report rows (pair + rule)",)
                and not r["Metric"].startswith("Non-")), "")
    return {"summary": summary, "detail": _read_csv(os.path.join(det, "fp_score_detail.csv")),
            "weights": _read_csv(os.path.join(det, "relevance_weights.csv")),
            "tolerance": _read_csv(os.path.join(det, "rule_tolerance.csv")),
            "results": [], "segment_label": seg}


# per-attribute comparator result -> colour token
RESULT_TONES = [("Agree", "good"), ("Close variant", "candidate"), ("Phonetic collision", "warning"),
                ("Fuzzy collision", "warning"), ("Partial", "serious"), ("Disagree", "critical"), ("Null", "neutral")]


def _score_section(sc: dict, include_values: bool) -> Tuple[List[str], List[str]]:
    """(html parts, key findings) for the comparator score analysis."""
    summary = sc["summary"]
    meta_cols = {"Section", "Metric", "Total", "Unique Pairs", "Attribute", "Result", "Flags Pair", "Note"}
    rules = [k for k in (summary[0].keys() if summary else []) if k not in meta_cols]
    get = lambda metric: next((r for r in summary if r.get("Metric") == metric), {})
    num = lambda v: _f(v) if v not in ("-", "") else None
    seg = sc.get("segment_label") or ""

    rows_total = int(num(get("Report rows (pair + rule)").get("Total")) or 0)
    fl = get("Comparator-flagged rows")
    flagged, flagged_pairs = int(num(fl.get("Total")) or 0), int(num(fl.get("Unique Pairs")) or 0)
    fl_seg = int(num(get(f"  of which {seg}").get("Total")) or 0) if seg else 0
    below = int(num(get("Rows scored below the lowest threshold").get("Total")) or 0)
    avg = num(get("Average relevance score").get("Total"))

    results = [r for r in summary if r.get("Section") == "Comparator results" and (num(r.get("Total")) or 0) > 0]
    triggers = [r for r in results if r.get("Flags Pair") and (num(r.get("Total")) or 0) > 0]
    triggers.sort(key=lambda r: -(num(r.get("Total")) or 0))
    findings = []
    if rows_total:
        top = ", ".join(f"<b>{_e(r['Attribute'])} {_e(r['Result'].lower())}</b> ({int(num(r['Total'])):,} rows)"
                        for r in triggers[:3])
        findings.append(f"Comparator scores flag <b>{flagged:,}</b> rule-level rows ({_pct(flagged, rows_total)}; "
                        f"{flagged_pairs:,} unique pairs) where a fuzzy attribute disagrees, is missing or only "
                        f"matched phonetically" + (f" - mostly {top}" if top else "") + ".")
    if below:
        findings.append(f"<b>{below:,}</b> rows score below the rule's lowest threshold in the independent "
                        f"re-scoring although Reltio returned them.")

    tiles = [_tile("Comparator-flagged rows", f"{flagged:,}", f"{_pct(flagged, rows_total)} of {rows_total:,} rows",
                   "serious"),
             _tile("Flagged unique pairs", f"{flagged_pairs:,}", "a pair flagged by several rules counts once")]
    if seg:
        tiles.append(_tile(f"{seg} share of flagged", _pct(fl_seg, flagged), f"{fl_seg:,} flagged {seg} rows"))
    tiles += [_tile("Below lowest threshold", f"{below:,}", "rows Reltio returned that re-score lower"),
              _tile("Average relevance", f"{avg:.3f}" if avg is not None else "-", "all report rows")]

    out = ['<h2 id="scores">Comparator score analysis <span class="kicker">false positives by attribute</span></h2>',
           '<p class="lede">Every attribute comparison behind each match was re-scored with the rule\'s comparator '
           '(e.g. Double Metaphone codes, edit distance) and a text similarity check. A row is flagged when a fuzzy '
           'attribute disagrees, is missing, or matched only phonetically while the names are far apart.</p>',
           f'<div class="tiles">{"".join(tiles)}</div>']

    # charts: one stacked bar per rule for every attribute that can flag a pair
    attrs = list(dict.fromkeys(r["Attribute"] for r in results if r.get("Flags Pair")))
    classes = [(res, tone, res, "") for res, tone in RESULT_TONES]
    cards = []
    for a in attrs:
        rows = []
        for ru in rules:
            c = Counter()
            for r in results:
                if r["Attribute"] == a and num(r.get(ru)) is not None:
                    c[r["Result"]] += int(num(r[ru]))
            if sum(c.values()):
                rows.append((ru, c))
        if rows:
            cards.append(f'<div class="card"><h3>{_e(a)} <span class="sub">comparator result by rule</span></h3>'
                         f'{_stacked_rows(rows, classes, normalize=True)}</div>')
    if cards:
        out.append(f'<div class="grid2">{"".join(cards)}</div>')

    # results matrix
    hdr = ["Attribute", "Result"] + rules + ["Total", "Unique pairs"]
    body = []
    for r in results:
        fp = r.get("Flags Pair") or ""
        tag = (f' <span class="badge">flags pair{"" if fp == "Y" else " in " + _e(fp)}</span>' if fp else "")
        body.append([f'<b>{_e(r["Attribute"])}</b>', f'{_e(r["Result"])}{tag}']
                    + [_cell(r.get(ru)) for ru in rules] + [_cell(r.get("Total")), _cell(r.get("Unique Pairs"))])
    out.append('<div class="card"><h3>Comparator results matrix <span class="sub">rule-level rows; "-" = attribute '
               'not used by the rule</span></h3>'
               + _table(hdr, body, num_cols=range(2, len(hdr)), cls="matrix") + '</div>')

    # rule metrics: every other section of the summary, grouped
    body = []
    for section in ("Pair counts", "False positives", "Relevance", "Bands", "Reltio relevance"):
        rows = [r for r in summary if r.get("Section") == section]
        if not rows:
            continue
        body.append([f'<b>{_e(section)}</b>'] + [""] * (len(rules) + 2))
        for r in rows:
            metric = r["Metric"]
            pct = "pct" if "%" in metric else ""
            body.append([_e(metric.strip())] + [_cell(r.get(ru), pct) for ru in rules]
                        + [_cell(r.get("Total"), pct), _cell(r.get("Unique Pairs"))])
    out.append('<div class="card"><h3>Metrics by rule</h3>'
               + _table(["Metric"] + rules + ["Total", "Unique pairs"], body, num_cols=range(1, len(rules) + 3),
                        cls="matrix sections") + '</div>')

    # tolerance + weights
    tol = sc.get("tolerance") or []
    if tol:
        body = [[f'<b>{_e(t["Rule"])}</b><div class="muted">{_e(t.get("Rule Type", ""))}</div>',
                 _e(t.get("Bands", "")).replace("; ", "<br>") or '<span class="muted">rule logic only</span>',
                 _cell(t.get("Lowest Threshold")), _cell(t.get("Score Headroom")),
                 _e(t.get("Attributes That Can Fail Alone", "")),
                 f'{_cell(t.get("Pairs Count"))}<div class="muted small">'
                 f'{_e(t.get("Attribute Pairs That Can Fail Together", ""))}</div>'] for t in tol
                if t.get("Rule") in rules]
        out.append('<div class="card"><h3>How much disagreement each rule tolerates <span class="sub">from the '
                   'configured weights and thresholds</span></h3>'
                   + _table(["Rule", "Bands", "Lowest threshold", "Headroom", "Can fail alone and still match",
                             "Attribute pairs that can fail together"], body, num_cols=(2, 3), cls="tol") + '</div>')
    weights = [w for w in (sc.get("weights") or []) if w.get("Rule") in rules]
    if weights:
        body = []
        for w in weights:
            still = w.get("Still Matches If Only This Fails", "")
            body.append([_e(w["Rule"]), f'<b>{_e(w["Attribute"])}</b>', _e(w.get("Operator", "")),
                         _e(w.get("Comparator", "")), _cell(w.get("Weight Used")), _cell(w.get("Weight Share"), "frac"),
                         _cell(w.get("Score If Only This Fails")),
                         f'<span class="pill {"pill-warn" if still == "Yes" else ""}">{_e(still)}</span>'])
        out.append('<details class="card" open><summary><b>Relevance weights by rule</b> <span class="muted">'
                   'weight, share of the rule\'s total weight and the score when only that attribute fails</span>'
                   '</summary>' + _table(["Rule", "Attribute", "Operator", "Comparator", "Weight", "Share",
                                          "Score if only this fails", "Still matches?"], body,
                                         num_cols=(4, 5, 6), cls="weights") + '</details>')

    # examples: flagged rows closest to the threshold
    det = [d for d in (sc.get("detail") or []) if _f(d.get("Margin Above Threshold")) is not None]
    det.sort(key=lambda d: (_f(d.get("Margin Above Threshold")), d.get("PairKey", "")))
    if det:
        hdr = ["Entities", "Rule", "Flagged by", "Relevance", "Margin", "Band", "Score impact"]
        if include_values:
            hdr.append("Values compared")
        body = []
        for d in det[:20]:
            row = [f'{_e(_rule(d.get("Source Entity")))}<br>{_e(_rule(d.get("Match Entity")))}', _e(d.get("Rule")),
                   _e(d.get("FP Triggers")), _cell(d.get("Relevance Score")), _cell(d.get("Margin Above Threshold")),
                   _e(d.get("Band")), _e(d.get("Score Impact"))]
            if include_values:
                row.append(_e(d.get("FP Reason")))
            body.append(row)
        out.append('<div class="card"><h3>Flagged rows closest to the threshold <span class="sub">smallest margin '
                   'first; full list in FP_Score_Report.xlsx</span></h3>'
                   + _table(hdr, body, num_cols=(3, 4), cls="top") + '</div>')
    out.append('<p class="muted">Result names, similarity, weights and tolerance terms are defined under '
               '<a href="#ref-results">Comparator results</a> and <a href="#ref-terms">Terms</a>.</p>')
    return out, findings


# ----------------------------------------------------------------------
# reference ("How to read this report"): every term the page uses
# ----------------------------------------------------------------------
OPERATOR_PLAIN = {
    "exact": "Values must be equal according to the attribute's comparator.",
    "fuzzy": "Same comparison as exact, but candidate pairs are found with approximate (e.g. phonetic) match tokens.",
    "exactOrNull": "Equal, or the value is missing on one or both records - a missing value counts as agreeing.",
    "exactOrAllNull": "Equal, or missing on both records; missing on only one record counts as disagreeing.",
    "notExact": "Values must differ.",
    "notExactSame": "Values must differ.",
    "multi": "A group of attributes compared together.",
    "in": "Filter: each record's value must be one of the listed values; carries no score.",
    "equals": "Filter: each record's value must equal the listed value; carries no score.",
    "notEquals": "Filter: neither record may hold one of the listed values; carries no score.",
    "notIn": "Filter: neither record may hold one of the listed values; carries no score.",
}
COMPARATOR_PLAIN = {
    "BasicStringComparator": "Exact text comparison after trimming (and ignoring case).",
    "DoubleMetaphoneComparator": "Phonetic comparison: values agree when they sound alike, i.e. share a Double "
                                 "Metaphone code (e.g. Smith / Smyth).",
    "DynamicDamerauLevenshteinDistance": "Typo-tolerant comparison: allows a number of character edits that grows "
                                         "with the length of the value.",
    "DamerauLevenshteinDistance": "Typo-tolerant comparison allowing a fixed number of character edits.",
    "JaroWinklerComparator": "Text similarity comparison: values agree above a similarity threshold.",
    "PhoneNumberComparator": "Phone number comparison on the digits of the number.",
    "CustomComparator": "Compares only the part of the value picked out by a configured pattern.",
    "CrossMultiComparator": "Compares a group of attributes, trying every way of pairing them up.",
    "AddressLineComparator": "Address line comparison tolerant of common abbreviations.",
}
# plain-language versions of pipeline.LIMITATIONS for the client page
LIMITATIONS_PLAIN = [
    "The independent re-evaluation imitates Reltio's matching from its documentation and the tenant "
    "configuration. It has not yet been checked against the tenant itself.",
    "The search for missed matches only compares records that share at least one similar value; duplicates that "
    "share none would not be found.",
    "Reltio's name, noise-word and alias dictionaries were not available, so values are compared without "
    "nickname or alias mapping.",
    "Working from the Potential Match Report alone, an empty value cannot be told apart from a missing one, and "
    "repeated attributes (e.g. several identifiers) lose their grouping.",
    "Only steward-confirmed pairs are certain; every other category is an assessment.",
]
RULE_TYPE_PLAIN = {
    "relevance_based": "Scores each pair from 0 to 1 and acts according to the score band (thresholds).",
    "suspect": "Pairs satisfying the rule are proposed as potential matches for review.",
    "automatic": "Pairs satisfying the rule are merged automatically.",
    "automatic_grouping": "Pairs satisfying the rule are grouped automatically (e.g. households).",
}


def _reference(*, params: dict, settings, fp_rows: List[dict], pair_cls: Counter, signal_pairs: Counter,
               rule_rows: List[dict], scores: Optional[dict], has_fn: bool, limitations: Sequence[str],
               exceptions_present: bool) -> List[str]:
    P = lambda k, d=None: params.get(k, getattr(settings, k, d))
    suspect_at, review_at = _f(P("fp_suspect_threshold", 5)), _f(P("fp_review_threshold", 2))
    weights = {k: _f(v) or 0.0 for k, v in (P("risk_weights", {}) or {}).items()}
    cutoff = _f(P("score_similarity_cutoff", 0.85))
    seg_label, seg_re = P("segment_label", ""), P("segment_crosswalk_regex", "")
    g = lambda x: f"{x:g}"

    # --- categories
    cat_rows = [
        [f'<span class="sw t-serious"></span> <b>Suspected false positive</b>', f"{g(suspect_at)} or more",
         "An attribute that Reltio compared actually disagrees (e.g. different last names), yet the pair still "
         "matched.", f"{pair_cls.get(SUSPECTED_FP, 0):,}"],
        [f'<span class="sw t-warning"></span> <b>Needs steward review</b>',
         f"{g(review_at)} to below {g(suspect_at)}",
         "Nothing clearly disagrees, but the match rests on weak evidence (missing or widely shared values, a "
         "score just above the threshold). It may be right or wrong - a person has to look.",
         f"{pair_cls.get(FP_REVIEW, 0):,}"],
        [f'<span class="sw t-neutral"></span> <b>No concerns found</b>', f"below {g(review_at)}",
         "No significant warning signs. Not proof that the match is correct.", f"{pair_cls.get(UNCLASSIFIED, 0):,}"],
        [f'<span class="sw t-critical"></span> <b>Confirmed false positive</b>', "steward decision",
         "A data steward marked the pair as not a match. Overrides the risk score.",
         f"{pair_cls.get(CONFIRMED_FP, 0):,}"],
        [f'<span class="sw t-good"></span> <b>Confirmed true positive</b>', "steward decision",
         "A data steward confirmed the pair as a real duplicate. Overrides the risk score.",
         f"{pair_cls.get(CONFIRMED_TP, 0):,}"],
    ]
    out = ['<h2 id="method">How to read this report</h2>',
           '<div class="card" id="ref-categories"><h3>Pair categories</h3>'
           '<p>All three assessed categories come from one <b>risk score</b>. Each warning sign found on a pair adds '
           'points, and the total decides the category. A pair returned by several rules takes its most serious '
           'category. A steward decision, when supplied, replaces the assessment.</p>'
           + _table(["Category", "Risk score", "In plain terms", "Pairs in this run"], cat_rows, num_cols=(3,),
                    cls="ref cats") + '</div>']

    # --- points per warning sign
    sig_rows = []
    for k, (label, meaning) in sorted(SIGNALS.items(), key=lambda kv: (-weights.get(kv[0], 0.0), kv[1][0])):
        sig_rows.append([f"<b>{_e(label)}</b>", g(weights.get(k, 0.0)), _e(meaning), f"{signal_pairs.get(k, 0):,}"])
    out.append('<div class="card" id="ref-signs"><h3>Points per warning sign</h3>'
               + _table(["Warning sign", "Points", "What it means", "Pairs showing it"], sig_rows, num_cols=(1, 3),
                        cls="ref")
               + f'<p class="muted">Points are configurable (RISK_WEIGHTS setting). Warning-sign thresholds used here: '
                 f'"widely shared" = held by at least {_e(P("high_frequency_entities", 5))} records and well above '
                 f'the attribute\'s typical frequency; "just above the threshold" = within '
                 f'{_e(P("low_margin", 0.03))} of the rule\'s lowest threshold.</p></div>')

    # --- worked examples: the most common combination in each assessed category
    combos: Dict[str, Counter] = defaultdict(Counter)
    for r in fp_rows:
        cls = r.get("Classification", "")
        if cls in (SUSPECTED_FP, FP_REVIEW, UNCLASSIFIED):
            keys = tuple(k for k in _split(r.get("RiskIndicators", "")) if k in SIGNALS)
            combos[cls][keys] += 1
    ex_rows = []
    for cls, label in ((SUSPECTED_FP, "Suspected false positive"), (FP_REVIEW, "Needs steward review"),
                       (UNCLASSIFIED, "No concerns found")):
        for keys, n in combos[cls].most_common(2):
            parts = [f"{SIGNALS[k][0]} ({g(weights.get(k, 0.0))})" for k in keys]
            total = sum(weights.get(k, 0.0) for k in keys)
            ex_rows.append([_e(" + ".join(parts) or "no warning signs"), g(total), _e(label), f"{n:,}"])
    if ex_rows:
        out.append('<div class="card" id="ref-examples"><h3>How the score adds up <span class="sub">the most common '
                   'combinations of warning signs in this run</span></h3>'
                   + _table(["Warning signs found", "Risk score", "Category", "Rule-level rows"], ex_rows,
                            num_cols=(1, 3), cls="ref") + '</div>')

    # --- comparator results
    if scores:
        ops = ", ".join(P("score_operators", ["fuzzy"]) or [])
        trig = set(P("score_triggers", []) or [])
        res_rows = [[f'<span class="sw t-{tone}"></span> <b>{_e(res)}</b>', _e(RESULT_MEANING.get(res, "")),
                     "Yes" if res in trig else "No"] for res, tone in RESULT_TONES]
        out.append('<div class="card" id="ref-results"><h3>Comparator results</h3>'
                   f'<p>Every attribute comparison behind a match gets one result. A row is <b>comparator-flagged</b> '
                   f'when an attribute compared with the <i>{_e(ops)}</i> operator gets a result marked "Yes" below. '
                   f'<b>Similarity</b> is a Jaro-Winkler text similarity from 0 (nothing in common) to 1 (identical), '
                   f'calculated independently of the rule; the cutoff is {g(cutoff)}.</p>'
                   + _table(["Result", "Meaning", "Flags the row"], res_rows, cls="ref") + '</div>')

    # --- missed matches
    fn_rows_ = [[f'<span class="sw t-{tone}"></span> <b>{_e(lbl)}</b>', _e(txt)] for _, tone, lbl, txt in FN_CLASSES]
    fn_rows_ += [[f"<b>Reason: {_e(v)}</b>", _e(k)] for k, v in FN_REASONS.items()] if has_fn else []
    out.append('<div class="card" id="ref-missed"><h3>Missed-match categories</h3>'
               '<p>A <b>missed match</b> (false negative) is a pair of records that satisfies a match rule but that '
               'Reltio did not propose. Finding them requires comparing records independently of Reltio, which needs '
               'an export of the entity records.</p>'
               + _table(["Category", "Meaning"], fn_rows_, cls="ref") + '</div>')

    # --- terms
    terms = [
        ("Potential-match pair", "Two records Reltio proposed as possible duplicates."),
        ("Rule-level row", "One pair under one match rule. A pair proposed by several rules appears once per rule."),
        ("Unique pair", "A pair counted once, however many rules proposed it."),
        ("Match rule", "A configured rule (Reltio match group) that decides when two records are duplicates."),
        ("Entities", "Reltio entity IDs of the two records in a pair."),
        ("Risk score", "Sum of the points of the warning signs found on a pair (see Points per warning sign)."),
        ("Re-evaluation agreement / Reproduced",
         "Share of Reltio's results that an independent re-evaluation of the configured rules also matches. It "
         "shows how well the explanations reflect Reltio's behaviour; it is not a verification by the tenant."),
        ("Relevance score", "For relevance-based rules: a 0-1 score, the weighted average of the attribute "
                            "comparison scores. 1 = every compared attribute agrees."),
        ("Match thresholds / Band", "Score ranges configured on a rule, each with a label (e.g. Strong matches). "
                                    "The band is the range a pair's score falls into."),
        ("Lowest threshold", "The bottom of the lowest band; a pair scoring below it is not matched."),
        ("Margin", "Relevance score minus the lowest threshold: how far the pair is above the cut-off."),
        ("Headroom", "1 minus the lowest threshold: how much score a pair can lose and still match."),
        ("Weight / Share", "How much an attribute counts in the relevance score, and its share of the rule's total."),
        ("Score if only this fails", "The relevance score when that one attribute disagrees and all others agree."),
        ("Still matches?", "Whether the pair is still matched when only that attribute disagrees."),
        ("Can fail alone / Attribute pairs that can fail together",
         "Attributes (or combinations of two) that can disagree while the pair still matches; the score with "
         "both failing is shown in brackets."),
        ("Score lost / Score impact", "How much higher the score would be if that attribute agreed."),
        ("Comparator", "The method a rule uses to compare an attribute's values (see Comparators used)."),
        ("Comparator detail", "What the comparator saw, e.g. Double Metaphone codes (MR vs ST) or the edit "
                              "distance and the edits allowed."),
        ("Comparator score (Rel)", "The comparator's own 0-1 score for the attribute."),
        ("Comparator-flagged row / FP rate", "A rule-level row with at least one flagging comparator result; FP rate "
                                             "= flagged rows as a percentage of all rows."),
        ("Steward decision", "A data steward's verdict on a pair (match / not a match). Only steward decisions "
                             "make a result confirmed."),
        ("Search coverage", "Share of the matches found by comparing every record with every other that the faster "
                            "candidate search also finds (measured on a small dataset)."),
    ]
    if seg_label:
        terms.append((seg_label, f"A pair where both records have a crosswalk matching {seg_re}, counted "
                                 f"separately to compare this population with the rest."))
    if exceptions_present:
        terms.append(("Processing notes", "Events recorded while processing (ERROR, WARNING, INFO), such as values "
                                          "the analyzer could not interpret; they qualify, not change, the results."))
    op_names = sorted({m for x in rule_rows for m in re.findall(r"(\w+)x\d+", x.get("Operators", ""))}
                      | {(w.get("Operator") or "").split(" ")[0] for w in (scores or {}).get("weights", []) or []}
                      - {""})
    for op in op_names:
        if op in OPERATOR_PLAIN:
            terms.append((f"Operator: {op}", OPERATOR_PLAIN[op]))
    types = sorted({x.get("Type", "") for x in rule_rows} - {""})
    for t in types:
        terms.append((f"Rule type: {t}", RULE_TYPE_PLAIN.get(t, "Reltio match group type.")))
    out.append('<div class="card" id="ref-terms"><h3>Terms</h3>'
               + _table(["Term", "Meaning"], [[f"<b>{_e(a)}</b>", _e(b)] for a, b in terms], cls="ref") + '</div>')

    cmps = sorted({m for x in rule_rows for m in re.findall(r"=(\w+)", x.get("ComparatorClasses", ""))})
    if cmps:
        from . import comparator_registry
        reg = comparator_registry.registered()
        rows = [[f"<b>{_e(c)}</b>", _e(COMPARATOR_PLAIN.get(c) or (reg[c].DOC.get("normalisation", "")
                                                                  if c in reg else "Comparator class."))]
                for c in cmps]
        out.append('<div class="card" id="ref-comparators"><h3>Comparators used by the analysed rules</h3>'
                   + _table(["Comparator", "How it compares"], rows, cls="ref") + '</div>')

    out.append('<div class="card" id="ref-limits"><h3>Limitations</h3><ul>'
               + "".join(f"<li>{_e(x)}</li>" for x in LIMITATIONS_PLAIN) + '</ul>'
               '<p class="muted">Technical wording of these limitations: Executive Summary sheet of the workbook.</p>'
               '</div>')
    return out


def _cell(v, pct: str = "") -> str:
    """Table cell for a number; pct 'pct' = value already in percent, 'frac' = fraction."""
    if v in (None, ""):
        return ""
    if v == "-":
        return '<span class="muted">-</span>'
    f = _f(v)
    if f is None:
        return _e(v)
    if pct:
        return f"{f:.1f}%" if pct == "pct" else f"{f:.1%}"
    return f"{int(f):,}" if f == int(f) and abs(f) >= 1 else f"{f:.3f}".rstrip("0").rstrip(".") if f else "0"


# ----------------------------------------------------------------------
# report
# ----------------------------------------------------------------------
def _lowest_threshold(action_thresholds: str) -> Optional[float]:
    los = [float(m) for m in re.findall(r"(\d+(?:\.\d+)?)\s*-\s*\d", action_thresholds or "")]
    return min(los) if los else None


def write(path: str, *, fp_rows: List[dict], fp_pairs: List[dict], fn_rows: List[dict], rule_rows: List[dict],
          recall_rows: List[dict], exceptions: List[dict], meta: dict, settings, limitations: Sequence[str],
          workbook: str = "", scores: Optional[dict] = None) -> str:
    params = meta.get("parameters") or {}
    tenant_props = params.get("tenant_properties") or getattr(settings, "tenant_properties", "")
    tp = load_properties(tenant_props) if tenant_props and os.path.isfile(tenant_props) else {}
    tenant = tp.get("TENANT_ID", "")
    env = tp.get("ENVIRONMENT_URL", "")
    etypes = sorted({r.get("EntityType", "") for r in fp_rows if r.get("EntityType")}) \
        or params.get("entity_types") or []
    run_at = (meta.get("finished_at") or meta.get("started_at") or "").replace("T", " ")
    has_fn = bool(fn_rows) or bool(meta.get("entity_stats"))

    # ---------------- aggregates ----------------
    rows_total, pairs_total = len(fp_rows), len(fp_pairs)
    pair_cls = Counter(p.get("Classification", "") for p in fp_pairs)
    reproduced = sum(1 for r in fp_rows if _b(r.get("PythonMatched")))
    by_rule: Dict[str, Counter] = defaultdict(Counter)
    rule_meta: Dict[str, dict] = {}
    rel_values: Dict[str, List[float]] = defaultdict(list)
    failed_by_rule: Dict[str, Counter] = defaultdict(Counter)
    constraint_by_rule: Dict[str, Counter] = defaultdict(Counter)
    for r in fp_rows:
        g = _rule(r.get("MatchGroupURI", ""))
        by_rule[g][r.get("Classification", "")] += 1
        m = rule_meta.setdefault(g, {"rows": 0, "reproduced": 0, "label": r.get("MatchGroupLabel") or g,
                                     "type": r.get("MatchGroupType", "")})
        m["rows"] += 1
        if _b(r.get("PythonMatched")):
            m["reproduced"] += 1
            for a in _split(r.get("FailedAttributes", "")):
                failed_by_rule[g][a] += 1
        rel = _f(r.get("PythonRelevance"))
        if rel is not None:
            rel_values[g].append(rel)
        if r.get("UnmetConstraintsIgnored"):
            constraint_by_rule[g][r["UnmetConstraintsIgnored"]] += 1
    signal_pairs = Counter(i for p in fp_pairs for i in _split(p.get("RiskIndicators", "")) if i not in INFORMATIONAL)
    rules_sorted = sorted(by_rule, key=lambda g: -sum(by_rule[g].values()))
    thresholds = {_rule(x.get("MatchGroupURI", "")): _lowest_threshold(x.get("ActionThresholds", ""))
                  for x in rule_rows}
    suspected = pair_cls.get(SUSPECTED_FP, 0) + pair_cls.get(CONFIRMED_FP, 0)
    review = pair_cls.get(FP_REVIEW, 0)
    labelled = pair_cls.get(CONFIRMED_FP, 0) + pair_cls.get(CONFIRMED_TP, 0)

    fn_pairs_by_cls: Dict[str, set] = defaultdict(set)
    fn_reasons = Counter()
    fn_by_rule: Dict[str, Counter] = defaultdict(Counter)
    for r in fn_rows:
        fn_pairs_by_cls[r.get("Classification", "")].add(r.get("PairKey"))
        fn_by_rule[_rule(r.get("MatchGroupURI", ""))][r.get("Classification", "")] += 1
        if r.get("Classification") not in (RELTIO_MATCH, EXCLUDED):
            for code in _split(r.get("ReasonCode", "")):
                fn_reasons[code] += 1
    fn_counts = Counter({k: len(v) for k, v in fn_pairs_by_cls.items()})
    missed = sum(fn_counts.get(k, 0) for k in (CONFIRMED_FN, SUSPECTED_FN, PY_CANDIDATE, FN_REVIEW))
    recall_all = next((x for x in recall_rows if x.get("MatchGroupURI") == "(all groups)"), {})

    # ---------------- key findings ----------------
    findings: List[str] = []
    if pairs_total:
        findings.append(f"Reltio proposed <b>{pairs_total:,}</b> potential-match pairs ({rows_total:,} rule-level "
                        f"rows). <b>{suspected:,}</b> ({_pct(suspected, pairs_total)}) carry strong warning signs of a "
                        f"false positive and <b>{review:,}</b> ({_pct(review, pairs_total)}) need a steward's decision.")
        worst = max(rules_sorted, key=lambda g: (by_rule[g][SUSPECTED_FP] / max(sum(by_rule[g].values()), 1),
                                                 sum(by_rule[g].values())))
        n = sum(by_rule[worst].values())
        findings.append(f"<b>{_e(worst)}</b> has the highest share of suspected false positives: "
                        f"{by_rule[worst][SUSPECTED_FP]:,} of its {n:,} rows ({_pct(by_rule[worst][SUSPECTED_FP], n)}).")
    by_cond: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for g in rules_sorted:
        for cond, n in constraint_by_rule[g].most_common():
            by_cond[cond].append((g, n))
    for cond, hits in by_cond.items():
        where = " and ".join(f"<b>{sum(n for _, n in hits[:1]):,}</b> rows in <b>{_e(g)}</b>" if i == 0 else
                             f"{n:,} in <b>{_e(g)}</b>" for i, (g, n) in enumerate(hits))
        findings.append(f"Reltio returned {where} although the rule's own condition <code>{_e(cond)}</code> is not "
                        f"met. Either the condition is not enforced for these rules or it is evaluated on different "
                        f"values; worth confirming with Reltio.")
    for g in rules_sorted[:4]:
        top = failed_by_rule[g].most_common(2)
        if top and rule_meta[g]["reproduced"]:
            a, n = top[0]
            extra = f", and {top[1][1]:,} on {_e(top[1][0])}" if len(top) > 1 else ""
            findings.append(f"In <b>{_e(g)}</b>, {n:,} {'match goes' if n == 1 else 'matches go'} through although <b>{_e(a)}</b> does not "
                            f"agree{extra} - the remaining attributes carry enough weight to reach the threshold.")
    if rows_total:
        findings.append(f"An independent re-evaluation of the configured rules reproduces <b>{_pct(reproduced, rows_total)}"
                        f"</b> of Reltio's results, so the explanations in this report reflect the tenant's behaviour "
                        f"closely (agreement, not tenant verification).")
    if has_fn:
        findings.append(f"<b>{missed:,}</b> pairs look like duplicates that Reltio did not propose (possible missed "
                        f"matches); {fn_counts.get(RELTIO_MATCH, 0):,} pairs were confirmed as already found.")
    else:
        findings.append("Missed-match (false negative) detection has not been run yet: it needs an export of the "
                        "entity records (see Next steps).")
    sec_scores: List[str] = []
    if scores and scores.get("summary"):
        include_values = params.get("html_include_values", getattr(settings, "html_include_values", False))
        sec_scores, score_findings = _score_section(scores, _b(include_values))
        findings[1:1] = score_findings
    if not labelled:
        findings.append("No steward decisions were supplied, so every false-positive figure is an assessment based "
                        "on warning signs, not a confirmed result.")

    # ---------------- sections ----------------
    tiles = [
        _tile("Potential-match pairs", f"{pairs_total:,}", f"{rows_total:,} rule-level rows from Reltio"),
        _tile("Suspected false positives", f"{suspected:,}", f"{_pct(suspected, pairs_total)} of pairs", "serious"),
        _tile("Need steward review", f"{review:,}", f"{_pct(review, pairs_total)} of pairs", "warning"),
        _tile("No concerns found", f"{pair_cls.get(UNCLASSIFIED, 0):,}",
              f"{_pct(pair_cls.get(UNCLASSIFIED, 0), pairs_total)} of pairs", "neutral"),
        _tile("Possible missed matches", f"{missed:,}" if has_fn else "Not run",
              "pairs Reltio did not propose" if has_fn else "requires entity export", "serious" if has_fn else ""),
        _tile("Re-evaluation agreement", _pct(reproduced, rows_total), "Reltio results reproduced independently"),
    ]

    sec_fp = [f'<h2 id="fp">Potential match quality <span class="kicker">false positives</span></h2>',
              '<p class="lede">Each pair Reltio proposed was re-evaluated against the configured rule and checked for '
              'warning signs. Pairs are grouped by the strength of those signs.</p>',
              '<div class="card"><h3>All potential-match pairs</h3>',
              _part_to_whole(pair_cls, FP_CLASSES),
              '<p class="muted">How the categories are scored: <a href="#ref-categories">Pair categories</a>.</p>'
              '</div>']
    if rules_sorted:
        sec_fp += ['<div class="card"><h3>By match rule <span class="sub">rule-level rows</span></h3>',
                   _stacked_rows([(g, by_rule[g]) for g in rules_sorted], FP_CLASSES), '</div>']
    sig_items = [(SIGNALS.get(k, (k, ""))[0], v, SIGNALS.get(k, ("", ""))[1]) for k, v in signal_pairs.most_common()]
    sec_fp += ['<div class="card"><h3>Warning signs found <span class="sub">pairs showing each sign; a pair can show '
               'several</span></h3>', _bar_list(sig_items, pairs_total),
               '<p class="muted">Points and meaning of each sign: <a href="#ref-signs">Points per warning sign</a>.</p>'
               '</div>']
    attr_cards = []
    for g in rules_sorted[:6]:
        items = [(a, n, "") for a, n in failed_by_rule[g].most_common(8)]
        if items:
            attr_cards.append(f'<div class="card"><h3>{_e(g)} <span class="sub">matched although this attribute '
                              f'differs</span></h3>{_bar_list(items, rule_meta[g]["reproduced"], "matches")}</div>')
    if attr_cards:
        sec_fp += ['<h3 class="group-title">Which attributes disagree in matched pairs</h3>',
                   f'<div class="grid2">{"".join(attr_cards)}</div>']
    hists = [_histogram(rel_values[g], thresholds.get(g), g) for g in rules_sorted[:6] if rel_values[g]]
    if hists:
        sec_fp += ['<h3 class="group-title">Match score distribution <span class="sub">relevance-based rules; scores '
                   'close to the threshold line are the least certain</span></h3>',
                   f'<div class="grid2">{"".join(hists)}</div>']

    sec_fn = ['<h2 id="fn">Missed matches <span class="kicker">false negatives</span></h2>']
    if has_fn:
        sec_fn += ['<p class="lede">Records were compared with each other independently of Reltio to find duplicate '
                   'pairs that satisfy a rule but are not in the Potential Match Report.</p>',
                   '<div class="card"><h3>Candidate pairs found by the independent comparison</h3>',
                   _part_to_whole(fn_counts, FN_CLASSES), '</div>']
        if fn_reasons:
            sec_fn += ['<div class="card"><h3>Why Reltio may have missed them <span class="sub">rule-level rows</span>'
                       '</h3>', _bar_list([(FN_REASONS.get(k, k), v, k) for k, v in fn_reasons.most_common()],
                                          unit="rows"), '</div>']
        fn_rules = sorted(fn_by_rule, key=lambda g: -sum(fn_by_rule[g].values()))
        if fn_rules:
            sec_fn += ['<div class="card"><h3>By match rule</h3>',
                       _stacked_rows([(g, fn_by_rule[g]) for g in fn_rules], FN_CLASSES), '</div>']
        cov = _f(recall_all.get("RecallOptimised"))
        if cov is not None:
            sec_fn.append('<div class="tiles">' + _tile("Search coverage", f"{cov:.1%}",
                                                        "of matches an all-pairs comparison finds (test data size)")
                          + '</div>')
    else:
        sec_fn += ['<div class="callout"><b>Not run in this analysis.</b> Finding missed matches requires an export of '
                   'the entity records, so that records Reltio never paired can be compared. The Potential Match '
                   'Report on its own only contains pairs Reltio already found. See Next steps and '
                   '<a href="#ref-missed">Missed-match categories</a>.</div>']

    def rule_cell(x: dict) -> str:
        desc = (x.get("Label") or "").split(":", 1)[-1].strip()
        return (f'<b>{_e(_rule(x.get("MatchGroupURI", "")))}</b> <span class="muted">{_e(x.get("Type", ""))}</span>'
                f'<div class="muted">{_e(desc)}</div>')

    def thresholds_cell(x: dict) -> str:
        parts = [re.sub(r"^potential_match\s*", "", p.strip()) for p in (x.get("ActionThresholds") or "").split(";")]
        return "<br>".join(_e(p) for p in parts if p) or '<span class="muted">rule logic only</span>'

    fn_rule_rows = lambda g: sum(fn_by_rule.get(g, Counter()).get(k, 0)
                                 for k in (CONFIRMED_FN, SUSPECTED_FN, PY_CANDIDATE))
    active_rows, idle_rows = [], []
    for x in rule_rows:
        g = _rule(x.get("MatchGroupURI", ""))
        m = rule_meta.get(g, {"rows": 0, "reproduced": 0})
        c = by_rule.get(g, Counter())
        if m["rows"] or fn_rule_rows(g):
            active_rows.append((m["rows"], [rule_cell(x), thresholds_cell(x), f'{m["rows"]:,}',
                                            _pct(m["reproduced"], m["rows"]) if m["rows"] else "-",
                                            f'{c.get(SUSPECTED_FP, 0):,}', f'{c.get(FP_REVIEW, 0):,}',
                                            f'{fn_rule_rows(g):,}' if has_fn else '<span class="muted">n/a</span>']))
        else:
            idle_rows.append([rule_cell(x), thresholds_cell(x)])
    type_counts = Counter(x.get("Type", "") for x in rule_rows)
    sec_rules = ['<h2 id="rules">Match rules</h2>',
                 f'<p class="lede">{len(rule_rows)} match rules analysed; {len(active_rows)} produced results in this '
                 f'analysis. Rule types: ' + ", ".join(f"{_e(k)} ({v})" for k, v in type_counts.most_common())
                 + '. Definitions: <a href="#ref-terms">Terms</a>.</p>',
                 _table(["Match rule", "Match thresholds", "Report rows", "Reproduced", "Suspected FP", "Review",
                         "Missed matches"], [r for _, r in sorted(active_rows, key=lambda t: -t[0])],
                        num_cols=(2, 3, 4, 5, 6), cls="rules")]
    if idle_rows:
        sec_rules.append(f'<details class="dataview"><summary>{len(idle_rows)} rules with no results in this '
                         f'analysis</summary>{_table(["Match rule", "Match thresholds"], idle_rows, cls="rules")}'
                         f'</details>')

    risk_order = lambda r: (-(_f(r.get("RiskScore")) or 0), r.get("PairKey", ""))
    flagged: Dict[str, List[dict]] = defaultdict(list)
    for r in fp_rows:
        if r.get("Classification") in (CONFIRMED_FP, SUSPECTED_FP):
            flagged[_rule(r.get("MatchGroupURI", ""))].append(r)
    per_rule = max(5, 25 // max(len(flagged), 1))      # every rule is represented, not only the largest
    top = sorted((r for rs in flagged.values() for r in sorted(rs, key=risk_order)[:per_rule]), key=risk_order)[:25]
    top_tbl = [[f'{_e(_rule(r.get("SourceURI")))}<br>{_e(_rule(r.get("MatchedURI")))}', _e(_rule(r.get("MatchGroupURI"))),
                f'{_f(r.get("RiskScore")) or 0:g}',
                _e("; ".join(SIGNALS[k][0] for k in _split(r.get("RiskIndicators", "")) if k in SIGNALS)),
                _e(r.get("FailedAttributes") or r.get("DifferingAttributes") or "")] for r in top]
    sec_top = ['<h2 id="top">Highest-risk pairs</h2>',
               '<p class="lede">The pairs with the strongest warning signs, taken from every rule - a good starting '
               'point for steward review. The workbook lists all of them with full explanations.</p>',
               _table(["Entities", "Rule", "Risk score", "Warning signs", "Attributes that disagree"], top_tbl,
                      num_cols=(2,), cls="top") if top_tbl else '<p class="empty">No suspected false positives.</p>']

    exc = Counter()
    for r in exceptions:
        exc[(r.get("Severity", ""), r.get("Category", ""))] += int(_f(r.get("Occurrences")) or 1)
    sec_exc = []
    if exc:
        sec_exc = ['<details class="card"><summary><b>Processing notes</b> '
                   f'<span class="muted">{sum(exc.values()):,} events in {len(exc)} categories</span></summary>',
                   _table(["Severity", "Category", "Events"], [[_e(s), _e(c), f"{n:,}"] for (s, c), n in
                                                               exc.most_common(15)], num_cols=(2,)),
                   '<p class="muted">Full list: <i>Processing Exceptions</i> sheet of the workbook.</p></details>']

    steps = []
    if not labelled:
        steps.append("Have data stewards review the highest-risk pairs and record decisions (MATCH / NOT_A_MATCH); "
                     "loading them turns suspected results into confirmed ones and measures precision.")
    if constraint_by_rule:
        steps.append("Confirm with Reltio how the identifier-type condition is applied in relevance-based rules, "
                     "using the pairs listed under <i>Rule's own condition not met</i>.")
    steps.append("Verify a sample of pairs directly against the tenant (read-only) to confirm the re-evaluation.")
    if not has_fn:
        steps.append("Export the entity records and run the missed-match analysis.")
    steps.append("Review rule weights and thresholds where matches go through despite disagreeing names.")

    sec_method = _reference(params=params, settings=settings, fp_rows=fp_rows, pair_cls=pair_cls,
                            signal_pairs=signal_pairs, rule_rows=rule_rows, scores=scores, has_fn=has_fn,
                            limitations=limitations, exceptions_present=bool(exc))

    nav = "".join(f'<a href="#{i}">{t}</a>' for i, t in (("findings", "Key findings"), ("fp", "False positives"))
                  + ((("scores", "Comparator scores"),) if sec_scores else ())
                  + (("fn", "Missed matches"), ("rules", "Match rules"),
                                                          ("top", "Highest-risk pairs"), ("next", "Next steps"),
                                                          ("method", "How to read")))
    sub = " · ".join(_e(x) for x in [f"Tenant {tenant}" if tenant else "", env, ", ".join(etypes),
                                     f"Run {run_at}" if run_at else ""] if x)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Match Quality Report</title>
<style>{CSS}</style></head>
<body><div class="page">
<header class="hero"><div class="eyebrow">Reltio match rule assessment</div>
<h1>False positive &amp; missed match analysis</h1><p class="meta">{sub}</p><nav>{nav}</nav></header>
<section class="tiles">{"".join(tiles)}</section>
<section id="findings" class="card findings"><h2>Key findings</h2><ul>{"".join(f"<li>{f}</li>" for f in findings)}</ul></section>
<section>{"".join(sec_fp)}</section>
<section>{"".join(sec_scores)}</section>
<section>{"".join(sec_fn)}</section>
<section>{"".join(sec_rules)}</section>
<section>{"".join(sec_top)}</section>
<section id="next"><h2>Next steps</h2><ol class="steps">{"".join(f"<li>{s}</li>" for s in steps)}</ol></section>
<section>{"".join(sec_method)}{"".join(sec_exc)}</section>
<footer>Generated by FPNP Analyzer {_e(meta.get("code_version", ""))}. Detailed results:
{_e(os.path.basename(workbook) or "FPNP_Analysis.xlsx")} (Executive Summary, FP/FN Candidates, Rule-Level Summary)
and FP_Score_Report.xlsx (comparator scores, relevance weights, rule tolerance).
Assessments are based on the configured rules and warning signs; only steward-labelled pairs are confirmed.</footer>
</div><div id="tip" role="tooltip"></div><script>{JS}</script></body></html>"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--track:#f0efec;--s1:#2a78d6;
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;--neutral:#c3c2b7;--candidate:#86b6ef;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--track:#262624;
--s1:#3987e5;--neutral:#5c5b57;--candidate:#5598e7;}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--track:#262624;--s1:#3987e5;--neutral:#5c5b57;
--candidate:#5598e7;}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.page{max-width:1120px;margin:0 auto;padding:32px 16px 48px}
h1{font-size:30px;line-height:1.2;margin:4px 0 6px;font-weight:650}
h2{font-size:21px;margin:40px 0 6px;font-weight:650}
h3{font-size:15px;margin:0 0 14px;font-weight:600}
.sub,.kicker{font-weight:400;color:var(--muted);font-size:13px;margin-left:6px}
.kicker{font-size:14px}
.eyebrow{color:var(--s1);font-weight:600;font-size:13px;letter-spacing:.04em;text-transform:uppercase}
.meta,.lede,.muted{color:var(--ink2)}
.muted{font-size:13px}
.lede{margin:0 0 16px;max-width:760px}
nav{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:14px;font-size:14px}
nav a{color:var(--ink2);text-decoration:none;border-bottom:1px solid var(--axis)}
nav a:hover{color:var(--ink)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin:0 0 16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin:24px 0 16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px;
border-top:3px solid var(--axis)}
.tile.tone-serious{border-top-color:var(--serious)}.tile.tone-warning{border-top-color:var(--warning)}
.tile.tone-neutral{border-top-color:var(--neutral)}
.tile-label{font-size:13px;color:var(--ink2)}.tile-value{font-size:28px;font-weight:650;margin:2px 0}
.tile-note{font-size:12px;color:var(--muted)}
.card>h2{margin-top:0}
.findings ul{margin:0;padding-left:20px}.findings li{margin:6px 0}
code{font-size:12.5px;background:var(--track);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
.stack{display:flex;gap:2px;height:28px;border-radius:6px;overflow:hidden}
.stack.thin{height:18px;border-radius:4px}
.seg{display:block;min-width:2px;outline:none}.seg:hover,.seg:focus{filter:brightness(1.1)}
.t-good{background:var(--good)}.t-warning{background:var(--warning)}.t-serious{background:var(--serious)}
.t-critical{background:var(--critical)}.t-neutral{background:var(--neutral)}.t-candidate{background:var(--candidate)}
.legend{list-style:none;margin:14px 0 0;padding:0;display:grid;gap:6px}
.legend li{display:grid;grid-template-columns:14px minmax(150px,auto) 80px 60px 1fr;gap:10px;align-items:baseline;
font-size:14px}
.legend.inline{display:flex;flex-wrap:wrap;gap:4px 18px;margin:0 0 12px}
.legend.inline li{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--ink2)}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;align-self:center}
.lg-val{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.lg-pct{text-align:right;color:var(--ink2);font-variant-numeric:tabular-nums}
.lg-note{color:var(--muted);font-size:13px}
.srows,.brows{display:grid;gap:8px}
.srow,.brow{display:grid;grid-template-columns:minmax(110px,190px) 1fr 84px;gap:12px;align-items:center;font-size:14px}
.srow-val,.brow-val{text-align:right;font-variant-numeric:tabular-nums}
.brow-val small{color:var(--muted);margin-left:6px;font-size:12px}
.brow-label{color:var(--ink2)}
.brow-track,.srow-track{height:18px;display:flex;align-items:center}
.bar{display:block;height:14px;background:var(--s1);border-radius:0 4px 4px 0;outline:none}
.bar:hover,.bar:focus{filter:brightness(1.12)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}
.grid2 .card{margin:0}
.group-title{margin:24px 0 12px;font-size:16px}
.hist{margin:0;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 20px}
.hist figcaption{font-weight:600;margin-bottom:6px}
.hist svg{width:100%;max-width:640px;height:auto;display:block}
.hbar{fill:var(--s1);outline:none}.hbar:hover,.hbar:focus{filter:brightness(1.15)}
.axis{stroke:var(--axis);stroke-width:1}.tick{fill:var(--muted);font-size:11px;text-anchor:middle}
.thr{stroke:var(--ink2);stroke-width:1.5}.thr-label{fill:var(--ink2);font-size:11px}
.dataview{margin-top:12px;font-size:13px}.dataview summary{color:var(--muted);cursor:pointer}
.tablewrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
.dataview .tablewrap{margin-top:8px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:9px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--grid)}
th{font-weight:600;color:var(--ink2);font-size:12.5px;white-space:nowrap;background:var(--track)}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.rules td:nth-child(2){min-width:170px;font-size:12.5px;color:var(--ink2)}
.rules td:first-child{min-width:260px}
.top td:first-child{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;white-space:nowrap}
.callout{background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--warning);
border-radius:8px;padding:14px 18px;max-width:820px}
.steps li{margin:6px 0;max-width:860px}
.cats td:nth-child(2){white-space:nowrap}.ref td:first-child{min-width:190px}
.ref .sw{vertical-align:-1px}
.gloss{margin:0;display:grid;grid-template-columns:minmax(150px,230px) 1fr;gap:8px 16px;font-size:13.5px}
.gloss dt{font-weight:600;display:flex;gap:8px;align-items:baseline}.gloss dd{margin:0;color:var(--ink2)}
details.card summary{cursor:pointer}
details.card[open] summary{margin-bottom:12px}
.badge{display:inline-block;font-size:11px;padding:0 6px;border-radius:9px;margin-left:4px;
background:var(--serious);color:#fff;white-space:nowrap}
.pill{display:inline-block;font-size:12px;padding:1px 8px;border-radius:9px;background:var(--track)}
.pill-warn{background:var(--warning);color:#0b0b0b}
.small{font-size:12px}
.matrix td,.matrix th{white-space:nowrap}
.sections td:first-child{white-space:normal}
.tol td:nth-child(6){min-width:260px}.tol td:nth-child(5){min-width:160px}.tol td:nth-child(2){min-width:170px}
.empty{color:var(--muted)}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--grid);color:var(--muted);font-size:12.5px}
#tip{position:fixed;pointer-events:none;z-index:10;max-width:340px;padding:8px 10px;border-radius:8px;
background:var(--ink);color:var(--surface);font-size:12.5px;line-height:1.4;opacity:0;transition:opacity .1s}
@media (max-width:640px){h1{font-size:24px}.legend li{grid-template-columns:14px 1fr 70px 52px}.lg-note{display:none}
.srow,.brow{grid-template-columns:96px 1fr 64px;gap:8px;font-size:13px}.grid2{grid-template-columns:1fr}
.gloss{grid-template-columns:1fr}.tile-value{font-size:24px}}
@media print{:root{color-scheme:light}body{background:#fff;font-size:12px}.page{max-width:none;padding:0}
nav,.dataview,#tip{display:none}.card,.hist,.tile,figure,tr{break-inside:avoid}h2{break-after:avoid}
details.card{display:block}}
"""

JS = """
(function(){var t=document.getElementById('tip');
function show(e){var el=e.target.closest('[data-tip]');if(!el){t.style.opacity=0;return;}
t.textContent=el.getAttribute('data-tip');t.style.opacity=1;var r=el.getBoundingClientRect();
var x=e.clientX!==undefined&&e.type!=='focusin'?e.clientX:r.left+r.width/2,y=e.clientY!==undefined&&e.type!=='focusin'?e.clientY:r.top;
var w=t.offsetWidth,h=t.offsetHeight;t.style.left=Math.max(8,Math.min(window.innerWidth-w-8,x-w/2))+'px';
t.style.top=(y-h-12<8?y+18:y-h-12)+'px';}
document.addEventListener('mousemove',show);document.addEventListener('focusin',show);
document.addEventListener('mouseleave',function(){t.style.opacity=0;});
document.addEventListener('focusout',function(){t.style.opacity=0;});})();
"""
