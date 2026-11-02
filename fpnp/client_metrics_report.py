"""
Client-presentable FP / FN metrics report.

  write_client_html(...)  self-contained HTML (no internet needed, prints to PDF from the browser):
                          headline tiles, plain-language findings, FP verdict mix, similarity
                          distribution, rule and attribute tables, FN confidence mix, missed-match
                          reasons, blocking coverage, method and caveats
  write_client_xlsx(...)  the same metrics as formatted Excel tables with native bar charts

Both read the shared metrics (reltio_engine.metrics), so they agree with the analysis workbooks.
Attribute values are never shown - only counts, rates and rule / attribute names - so the report
can be shared outside the data team.
"""

import html
import os
import time
from typing import List, Optional, Sequence

from .reltio_engine.metrics import FN_SEVERITY, FP_SEVERITY, fn_metrics, fp_metrics, pct

E = html.escape


def _n(v) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, float):
        return f"{v:,.1f}"
    return f"{v:,}" if isinstance(v, int) else E(str(v))


def _p(v) -> str:
    return "–" if v is None else f"{v * 100:.1f}%"


def _tile(label: str, value: str, note: str = "") -> str:
    return (f'<div class="tile"><div class="tile-label">{E(label)}</div><div class="tile-value">{value}</div>'
            + (f'<div class="tile-note">{E(note)}</div>' if note else "") + "</div>")


def _bars(items: Sequence[tuple], unit: str = "pairs", total: Optional[int] = None) -> str:
    """Horizontal bar list: (label, value, note). One hue, every bar labelled, hover tooltip."""
    items = [i for i in items if i[1]]
    if not items:
        return '<p class="muted">No data.</p>'
    mx = max(v for _, v, *_ in items) or 1
    tot = total if total is not None else sum(v for _, v, *_ in items)
    out = ['<div class="bars" role="list">']
    for label, v, *rest in items:
        note = rest[0] if rest else ""
        share = f" ({v / tot:.1%})" if tot else ""
        tip = f"{label}: {v:,} {unit}{share}" + (f" – {note}" if note else "")
        out.append(f'<div class="bar-row" role="listitem" data-tip="{E(tip)}">'
                   f'<div class="bar-label">{E(label)}</div>'
                   f'<div class="bar-track"><div class="bar" style="width:{max(0.6, 100 * v / mx):.1f}%"></div></div>'
                   f'<div class="bar-value">{v:,}<span class="muted">{share}</span></div></div>')
    out.append("</div>")
    return "".join(out)


def _nice_step(mx: int) -> int:
    """Tick step giving 2-5 integer gridlines (1, 2, 5 x 10^n)."""
    raw = mx / 4
    mag = 10 ** max(0, len(str(int(raw))) - 1)
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def _histogram(bins: Sequence[dict], title: str) -> str:
    rows = [b for b in bins if b["ScoreRange"] != "Not scored"]
    if not any(b["Pairs"] for b in rows):
        return '<p class="muted">No scored pairs.</p>'
    mx = max(b["Pairs"] for b in rows) or 1
    w, h, pad_l, pad_b = 640, 220, 36, 34
    bw = (w - pad_l) / len(rows)
    parts = [f'<svg class="hist" viewBox="0 0 {w} {h}" role="img" aria-label="{E(title)}">']
    step = max(1, _nice_step(mx))
    mx = max(mx, step * -(-mx // step))                   # axis top = next multiple of the tick step
    for v in range(0, mx + 1, step):
        y = (h - pad_b) * (1 - v / mx) + 4 if v else h - pad_b
        parts.append(f'<line x1="{pad_l}" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                     f'<text x="{pad_l - 6}" y="{y + 4:.1f}" class="axis" text-anchor="end">{v:,}</text>')
    for k, b in enumerate(rows):
        bh = (h - pad_b - 4) * b["Pairs"] / mx
        x = pad_l + k * bw + 2
        y = h - pad_b - bh
        tip = f'Score {b["ScoreRange"]}: {b["Pairs"]:,} pairs ({_p(b["Share"])})'
        if b["Pairs"]:
            parts.append(f'<path class="col" data-tip="{E(tip)}" d="M{x:.1f},{h - pad_b} V{y + 4:.1f} '
                         f'Q{x:.1f},{y:.1f} {x + 4:.1f},{y:.1f} H{x + bw - 8:.1f} Q{x + bw - 4:.1f},{y:.1f} '
                         f'{x + bw - 4:.1f},{y + 4:.1f} V{h - pad_b} Z"/>')
            parts.append(f'<text x="{x + (bw - 4) / 2:.1f}" y="{y - 5:.1f}" class="val" text-anchor="middle">'
                         f'{b["Pairs"]:,}</text>')
        parts.append(f'<text x="{x + (bw - 4) / 2:.1f}" y="{h - pad_b + 16}" class="axis" text-anchor="middle">'
                     f'{E(b["ScoreRange"])}</text>')
    parts.append(f'<line x1="{pad_l}" x2="{w}" y1="{h - pad_b}" y2="{h - pad_b}" class="baseline"/></svg>')
    return "".join(parts)


def _table(cols: Sequence[tuple], rows: Sequence[dict], limit: int = 25) -> str:
    """cols: (key, header, kind) kind in '', 'n', 'p'."""
    if not rows:
        return '<p class="muted">No data.</p>'
    head = "".join(f'<th class="{"num" if k else ""}">{E(h)}</th>' for _, h, k in cols)
    body = []
    for r in rows[:limit]:
        tds = []
        for key, _, kind in cols:
            v = r.get(key)
            tds.append(f'<td class="num">{_p(v) if kind == "p" else _n(v)}</td>' if kind else f"<td>{_n(v)}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    more = f'<p class="muted">{len(rows) - limit} more rows in the analysis workbook.</p>' if len(rows) > limit else ""
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>{more}'


CSS = """
:root{--surface-0:#f4f4f1;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#76756f;
--rule:#e2e1dc;--grid:#ecebe7;--series-1:#2a78d6;--accent:#1f3a5f;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--surface-0:#121211;--surface-1:#1a1a19;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#9a9990;--rule:#33332f;--grid:#2a2a27;--series-1:#3987e5;
--accent:#9ec5f4;color-scheme:dark}}
:root[data-theme="dark"]{--surface-0:#121211;--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;
--text-muted:#9a9990;--rule:#33332f;--grid:#2a2a27;--series-1:#3987e5;--accent:#9ec5f4;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--surface-0);color:var(--text-primary);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 16px 64px}
header h1{font-size:28px;margin:0 0 4px;color:var(--accent)}header p{margin:0;color:var(--text-secondary)}
section{background:var(--surface-1);border:1px solid var(--rule);border-radius:10px;padding:20px 22px;margin-top:20px}
h2{font-size:19px;margin:0 0 4px}h3{font-size:15px;margin:18px 0 8px;color:var(--text-secondary)}
.lede{color:var(--text-secondary);margin:0 0 14px}.muted{color:var(--text-muted);font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:14px}
.tile{border:1px solid var(--rule);border-radius:8px;padding:12px 14px;background:var(--surface-1)}
.tile-label{font-size:12px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.04em}
.tile-value{font-size:28px;font-weight:650;margin-top:2px}.tile-note{font-size:12px;color:var(--text-muted)}
.findings li{margin:4px 0}
.bars{display:grid;gap:6px}.bar-row{display:grid;grid-template-columns:minmax(120px,260px) 1fr 110px;gap:10px;
align-items:center;padding:2px 0}.bar-label{font-size:14px}.bar-track{height:14px}
.bar{height:14px;background:var(--series-1);border-radius:0 4px 4px 0}.bar-value{font-variant-numeric:tabular-nums;
text-align:right;font-size:14px}.bar-value .muted{margin-left:4px}
.bar-row:hover .bar,.col:hover{opacity:.8}
svg.hist{width:100%;height:auto;max-width:640px}.col{fill:var(--series-1)}.grid{stroke:var(--grid);stroke-width:1}
.baseline{stroke:var(--text-muted);stroke-width:1}.axis{fill:var(--text-muted);font-size:11px}
.val{fill:var(--text-secondary);font-size:11px}
.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:6px 8px;border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}
th{color:var(--text-secondary);font-weight:600}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.two{display:grid;grid-template-columns:1fr 1fr;gap:24px}@media(max-width:760px){.two{grid-template-columns:1fr}
.bar-row{grid-template-columns:1fr 90px}.bar-track{grid-column:1/-1;order:3}}
#tip{position:fixed;pointer-events:none;background:var(--text-primary);color:var(--surface-1);padding:5px 8px;
border-radius:6px;font-size:12.5px;display:none;max-width:320px;z-index:9}
@media print{body{background:#fff}section{break-inside:avoid;border-color:#ccc}#tip{display:none!important}}
"""

TIP_JS = """<div id="tip" role="tooltip"></div><script>
(function(){var t=document.getElementById('tip');document.addEventListener('mousemove',function(e){
var el=e.target.closest('[data-tip]');if(!el){t.style.display='none';return;}t.textContent=el.getAttribute('data-tip');
t.style.display='block';var x=Math.min(e.clientX+14,window.innerWidth-t.offsetWidth-8);t.style.left=x+'px';
t.style.top=(e.clientY+14)+'px';});})();</script>"""


def _fp_section(m: dict) -> str:
    cls = m["classification_rows"]
    ev = m["evaluated"] or 1
    pot = cls["Potential False Positive"] + cls["Confirmed False Positive"]
    gen = cls["Likely Genuine Duplicate"] + cls["Confirmed Genuine Duplicate"]
    rev = cls["Requires Manual Review"]
    tiles = "".join([
        _tile("Potential-match pairs", _n(m["unique_pairs"]), f"{_n(m['rows'])} pair-rule rows analysed"),
        _tile("Likely genuine duplicates", _p(pct(gen, ev)), f"{gen:,} rows"),
        _tile("Potential false positives", _p(pct(pot, ev)), f"{pot:,} rows (incl. confirmed)"),
        _tile("Needs steward review", _p(pct(rev, ev)), f"{rev:,} rows"),
        _tile("Avg estimated similarity", _n(m["score_stats"]["Avg"]), "0–100, analytical"),
        _tile("Rule logic reproduced", _p(pct(m["reproduced"], ev)), "Python model vs Reltio output"),
    ])
    worst = sorted([r for r in m["rules"] if r["Rows"] >= 1], key=lambda r: -(r["PotentialFPRate"] or 0))[:3]
    find = []
    if worst and (worst[0]["PotentialFPRate"] or 0) > 0:
        find.append("Highest potential false-positive rates: " + "; ".join(
            f"<b>{E(r['MatchRule'])}</b> {_p(r['PotentialFPRate'])} of {r['Rows']:,} rows" for r in worst
            if r["PotentialFPRate"]) + ".")
    if m["unmatched_attributes"]:
        find.append("Attributes that most often disagree inside returned pairs: " + ", ".join(
            f"<b>{E(k)}</b> ({v:,})" for k, v in m["unmatched_attributes"][:4]) + ".")
    if m["risk_indicators"]:
        find.append("Most frequent risk signals: " + ", ".join(
            f"{E(k.replace('_', ' ').lower())} ({v:,})" for k, v in m["risk_indicators"][:4]) + ".")
    if m["evaluated"] and m["reproduced"] < m["evaluated"]:
        find.append(f"{m['evaluated'] - m['reproduced']:,} returned rows are not reproduced by the Python rule "
                    "model; they are kept for review rather than labelled.")
    cls_items = [(c, cls[c]) for c in FP_SEVERITY if cls[c]]
    attr_items = [(a["Attribute"], a["NoMatch"] + a["PartialNearMiss"],
                   f"disagree rate {_p(a['DisagreeRate'])}") for a in m["attributes"]]
    attr_items = sorted([x for x in attr_items if x[1]], key=lambda x: -x[1])[:10]
    rules = _table([("MatchRule", "Match rule", ""), ("RuleType", "Type", ""), ("Rows", "Rows", "n"),
                    ("SimilarityAvg", "Avg similarity", "n"), ("ReproductionRate", "Reproduced", "p"),
                    ("Likely Genuine Duplicate", "Likely genuine", "n"), ("Requires Manual Review", "Review", "n"),
                    ("Potential False Positive", "Potential FP", "n"), ("PotentialFPRate", "Potential FP %", "p")],
                   m["rules"])
    return f"""<section id="fp"><h2>False positive analysis</h2>
<p class="lede">Every pair Reltio returned as a potential match, re-compared attribute by attribute with the
configured match rules and comparators.</p><div class="tiles">{tiles}</div>
<h3>Key findings</h3><ul class="findings">{''.join(f'<li>{f}</li>' for f in find) or '<li>No findings.</li>'}</ul>
<div class="two"><div><h3>Verdict mix (rows)</h3>{_bars(cls_items, 'rows')}</div>
<div><h3>Estimated similarity distribution</h3>{_histogram(m['score_bins'], 'Estimated similarity distribution')}</div></div>
<h3>Results by match rule</h3>{rules}
<h3>Attributes that disagree in returned pairs</h3>{_bars(attr_items, 'comparisons')}</section>"""


def _fn_section(m: dict) -> str:
    tiles = "".join([
        _tile("Entities analysed", _n(m["entities"])),
        _tile("Candidate pairs compared", _n(m["pairs_evaluated"]), f"{_n(m['candidate_pairs'])} generated by blocking"),
        _tile("High-confidence missed", _n(m["high"]), "rule satisfied, documented logic"),
        _tile("Medium-confidence missed", _n(m["medium"])),
        _tile("Needs manual review", _n(m["review"])),
        _tile("Near misses (low)", _n(m["low"]), "high similarity, rule not met"),
    ])
    conf_items = [(c, m["confidence"][c]) for c in FN_SEVERITY if m["confidence"][c]]
    reason_label = {"TOKENIZATION_MISMATCH": "No shared match token (never compared)",
                    "MATCH_EXPECTED_NOT_RETURNED": "Tokens shared, rule met, not returned",
                    "CANDIDATE_GENERATION_MISS": "Over-collided token (skipped)",
                    "RULE_NOT_SATISFIED": "Rule not satisfied (near miss)",
                    "STEWARD_CONFIRMED": "Steward-confirmed duplicate"}
    reasons = [(reason_label.get(k, k), v) for k, v in m["reasons"]]
    rules = _table([("MatchRule", "Match rule", ""), ("Candidates", "Candidates", "n"),
                    ("SatisfyRule", "Satisfy rule", "n"),
                    ("High-confidence potential false negative", "High", "n"),
                    ("Medium-confidence potential false negative", "Medium", "n"),
                    ("Requires manual review", "Review", "n"), ("Low-confidence candidate", "Low", "n")], m["rules"])
    blk = sorted(m["blocking"], key=lambda r: -(r.get("MatchingCandidates") or 0))
    blocking = _table([("BlockingKey", "Blocking key", ""), ("BlockingStrategy", "Strategy", ""),
                       ("NumberOfEntities", "Entities", "n"), ("CandidatePairsGenerated", "Pairs", "n"),
                       ("MatchingCandidates", "Matching", "n"), ("OversizedBlocksSkipped", "Blocks skipped", "n")],
                      blk, 15)
    excl = "".join(f"<li>{E(k)}: {v:,} pairs</li>" for k, v in (m["excluded"] or {}).items())
    return f"""<section id="fn"><h2>False negative analysis</h2>
<p class="lede">Entity exports (golden OV values only) searched for pairs that satisfy the configured rules but were
not matched in Reltio. All-pairs comparison is avoided with blocking keys derived from the rules.</p>
<div class="tiles">{tiles}</div>
<div class="two"><div><h3>Confidence mix</h3>{_bars(conf_items, 'pairs')}</div>
<div><h3>Why the pairs were likely missed</h3>{_bars(reasons, 'pairs')}</div></div>
<h3>Candidates by match rule</h3>{rules}
<h3>Similarity of reported candidates</h3>{_histogram(m['score_bins'], 'FN candidate similarity')}
<h3>Blocking coverage</h3>{blocking}
{f'<h3>Excluded before evaluation</h3><ul>{excl}</ul>' if excl else ''}</section>"""


def write_client_html(path: str, fp: Optional[tuple] = None, fn: Optional[tuple] = None,
                      title: str = "Reltio Match Quality Report", subtitle: str = "") -> str:
    """fp: (pairs, attributes, stats); fn: (pairs, attributes, blocking, stats)."""
    secs = []
    nav = []
    if fp is not None:
        secs.append(_fp_section(fp_metrics(*fp)))
        nav.append('<a href="#fp">False positives</a>')
    if fn is not None:
        secs.append(_fn_section(fn_metrics(*fn)))
        nav.append('<a href="#fn">False negatives</a>')
    method = """<section id="method"><h2>Method and how to read the numbers</h2><ul>
<li><b>Estimated similarity (0–100)</b> is an analytical score computed from the attributes each match rule uses,
with the comparator class configured for that attribute. It is <b>not</b> Reltio's internal match score.</li>
<li><b>Rule evaluation</b> is a Python model of the configured Reltio rule; it is reported separately from the
similarity – a pair can be similar and still fail the rule.</li>
<li>Labels such as <i>potential false positive</i> or <i>high-confidence missed match</i> are hypotheses for data
steward review. Only steward-confirmed pairs are reported as confirmed.</li>
<li>Comparators and operators whose exact Reltio behaviour is not documented are implemented as labelled
approximations; results depending on them are graded down in confidence.</li>
<li>Python results are not claimed to reproduce Reltio until a sample has been verified on the tenant.</li></ul></section>"""
    body = "".join(secs) or '<section><p class="muted">No results.</p></section>'
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)}</title><style>{CSS}</style></head>
<body><main><header><h1>{E(title)}</h1><p>{E(subtitle or 'Generated ' + time.strftime('%d %B %Y'))}
{(' · ' + ' · '.join(nav)) if nav else ''}</p></header>{body}{method}</main>{TIP_JS}</body></html>"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


# ----------------------------------------------------------------------
def write_client_xlsx(path: str, fp: Optional[tuple] = None, fn: Optional[tuple] = None,
                      title: str = "Reltio Match Quality Metrics") -> str:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws0 = wb.active
    ws0.title = "Overview"
    hdr_font, hdr_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F3A5F")

    def table(ws, r0, headers, rows, pct_cols=()):
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row=r0, column=c, value=h)
            cell.font, cell.fill = hdr_font, hdr_fill
        for i, row in enumerate(rows, 1):
            for c, v in enumerate(row, 1):
                cell = ws.cell(row=r0 + i, column=c, value=v)
                if headers[c - 1] in pct_cols and isinstance(v, float):
                    cell.number_format = "0.0%"
        for c in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(c)].width = max(14, min(48, max(
                [len(str(headers[c - 1]))] + [len(str(r[c - 1])) for r in rows]) + 2))
        return r0 + len(rows)

    def chart(ws, anchor, title_, cats_ref, vals_ref):
        ch = BarChart()
        ch.type, ch.style, ch.title, ch.legend = "bar", 10, title_, None
        ch.add_data(vals_ref, titles_from_data=True)
        ch.set_categories(cats_ref)
        ch.series[0].graphicalProperties.solidFill = "2A78D6"
        ch.height, ch.width = 7, 16
        ws.add_chart(ch, anchor)

    ws0["A1"] = title
    ws0["A1"].font = Font(bold=True, size=16, color="1F3A5F")
    ws0["A2"] = ("Estimated similarity is an analytical score, not Reltio's match score. Non-confirmed "
                 "classifications are hypotheses for steward review.")
    ws0["A2"].font = Font(italic=True, color="555555")
    r = 4
    if fp is not None:
        m = fp_metrics(*fp)
        ev = m["evaluated"] or 1
        cls = m["classification_rows"]
        kpis = [("Potential-match pairs (unique)", m["unique_pairs"]), ("Rows analysed", m["evaluated"]),
                ("Avg estimated similarity", m["score_stats"]["Avg"]),
                ("Rule logic reproduced", pct(m["reproduced"], ev))]
        kpis += [(c, cls[c]) for c in FP_SEVERITY if cls[c]]
        ws0.cell(row=r, column=1, value="False positive analysis").font = Font(bold=True, size=13)
        r = table(ws0, r + 1, ["Metric", "Value"], kpis) + 2
        ws = wb.create_sheet("FP Metrics")
        rows = [(c, cls[c], pct(cls[c], ev)) for c in FP_SEVERITY if cls[c]]
        end = table(ws, 1, ["Classification", "Rows", "Share"], rows, ("Share",))
        if rows:
            chart(ws, "E1", "FP verdict mix", Reference(ws, min_col=1, min_row=2, max_row=end),
                  Reference(ws, min_col=2, min_row=1, max_row=end))
        start = end + 18
        bins = [(b["ScoreRange"], b["Pairs"], b["Share"]) for b in m["score_bins"]]
        end2 = table(ws, start, ["Score range", "Rows", "Share"], bins, ("Share",))
        chart(ws, f"E{start}", "Estimated similarity distribution",
              Reference(ws, min_col=1, min_row=start + 1, max_row=end2),
              Reference(ws, min_col=2, min_row=start, max_row=end2))
        ws_r = wb.create_sheet("FP by Rule")
        keys = ["MatchRule", "RuleType", "Rows", "UniquePairs", "SimilarityAvg", "ReproductionRate",
                "Likely Genuine Duplicate", "Requires Manual Review", "Potential False Positive",
                "Confirmed False Positive", "Confirmed Genuine Duplicate", "PotentialFPRate"]
        table(ws_r, 1, keys, [tuple(x.get(k) for k in keys) for x in m["rules"]], ("ReproductionRate", "PotentialFPRate"))
        ws_a = wb.create_sheet("FP by Attribute")
        keys = ["Attribute", "Comparators", "Comparisons", "Exact", "AcceptedVariant", "PartialNearMiss", "NoMatch",
                "BothNull", "OneNull", "Conflicts", "MatchRate", "DisagreeRate", "NullRate", "AvgAttributeScore"]
        table(ws_a, 1, keys, [tuple(x.get(k) for k in keys) for x in m["attributes"]],
              ("MatchRate", "DisagreeRate", "NullRate"))
    if fn is not None:
        m = fn_metrics(*fn)
        kpis = [("Entities analysed", m["entities"]), ("Candidate pairs generated", m["candidate_pairs"]),
                ("Candidate pairs evaluated", m["pairs_evaluated"])]
        kpis += [(c, m["confidence"][c]) for c in FN_SEVERITY if m["confidence"][c]]
        kpis += [(f"Excluded: {k}", v) for k, v in (m["excluded"] or {}).items()]
        ws0.cell(row=r, column=1, value="False negative analysis").font = Font(bold=True, size=13)
        r = table(ws0, r + 1, ["Metric", "Value"], kpis) + 2
        ws = wb.create_sheet("FN Metrics")
        rows = [(c, m["confidence"][c]) for c in FN_SEVERITY if m["confidence"][c]]
        end = table(ws, 1, ["Confidence", "Pairs"], rows)
        if rows:
            chart(ws, "E1", "FN confidence mix", Reference(ws, min_col=1, min_row=2, max_row=end),
                  Reference(ws, min_col=2, min_row=1, max_row=end))
        start = end + 18
        end2 = table(ws, start, ["Reason", "Pairs"], list(m["reasons"]))
        if m["reasons"]:
            chart(ws, f"E{start}", "Reasons for missed matches",
                  Reference(ws, min_col=1, min_row=start + 1, max_row=end2),
                  Reference(ws, min_col=2, min_row=start, max_row=end2))
        ws_r = wb.create_sheet("FN by Rule")
        keys = ["MatchRule", "Candidates", "SatisfyRule"] + FN_SEVERITY[:-1]
        table(ws_r, 1, keys, [tuple(x.get(k) for k in keys) for x in m["rules"]])
        ws_b = wb.create_sheet("FN Blocking")
        keys = ["BlockingKey", "BlockingStrategy", "NumberOfEntities", "CandidatePairsGenerated",
                "MatchingCandidates", "OversizedBlocksSkipped"]
        table(ws_b, 1, keys, [tuple(x.get(k) for k in keys) for x in m["blocking"]])
    ws0.column_dimensions["A"].width = 52
    ws0.column_dimensions["B"].width = 18
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    wb.save(path)
    return path


def write_client_reports(out_dir: str, fp_res=None, fn_res=None, title: str = "Reltio Match Quality Report") -> dict:
    fp = (fp_res.pairs, fp_res.attributes, dict(fp_res.stats)) if fp_res is not None else None
    fn = (fn_res.pairs, fn_res.attributes, fn_res.blocking, fn_res.stats) if fn_res is not None else None
    return {"client_html": write_client_html(os.path.join(out_dir, "Reltio_Match_Quality_Report.html"), fp, fn, title),
            "client_xlsx": write_client_xlsx(os.path.join(out_dir, "Reltio_Match_Quality_Metrics.xlsx"), fp, fn)}
