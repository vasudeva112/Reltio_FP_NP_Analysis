"""
Reads Potential Match Reports.

Primary format: the CSV written by the existing extractor (Potential_Matches_Extract_v3.py) -
parsed by the existing report_reader so behaviour is identical to the `fp` CLI mode:
  Rule, RuleLabel, src_/match_ReltioURI, src_/match_Crosswalks, src_/match_<dotted attribute> ...,
  ReltioRelevance, ReltioMatchAction

Also accepted (same column conventions):
  * .xlsx / .xls copies of the report (first sheet, or every sheet that has the URI columns)
  * reports without a Rule column (e.g. single-rule extracts) - the rule is then taken from
    `default_rule`, or inferred per pair as the configured rule that best explains it
  * alias URI columns: EntityA/EntityB, Entity1/Entity2, EntityURI1/EntityURI2, SourceURI/MatchedURI
"""

import csv
import os
import tempfile
from typing import Iterable, Iterator, List, Optional, Sequence

from ..exceptions_log import LOG
from ..report_reader import ReportRow, read_reports

INFER_RULE = "(infer)"
URI_ALIASES = [("src_ReltioURI", "match_ReltioURI"), ("EntityA", "EntityB"), ("Entity1", "Entity2"),
               ("EntityURI1", "EntityURI2"), ("SourceURI", "MatchedURI"), ("Entity A ID", "Entity B ID")]


def _normalise_columns(header: Sequence[str]) -> dict:
    """Maps alias URI columns onto src_ReltioURI / match_ReltioURI."""
    cols = [c.strip() for c in header]
    if "src_ReltioURI" in cols and "match_ReltioURI" in cols:
        return {}
    for a, b in URI_ALIASES[1:]:
        if a in cols and b in cols:
            return {a: "src_ReltioURI", b: "match_ReltioURI"}
    return {}


def _rewrite_csv(rows: Iterable[dict], header: List[str], default_rule: str, out_path: str) -> str:
    rename = _normalise_columns(header)
    new_header = [rename.get(c, c) for c in header]
    if "Rule" not in new_header:
        new_header = ["Rule"] + new_header
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=new_header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            d = {rename.get(k, k): ("" if v is None else str(v)) for k, v in r.items()}
            if not d.get("Rule"):
                d["Rule"] = default_rule or INFER_RULE
            w.writerow(d)
    return out_path


def _needs_rewrite(header: Sequence[str]) -> bool:
    return "Rule" not in header or bool(_normalise_columns(header))


def _excel_to_csvs(path: str, default_rule: str, tmp: str) -> List[str]:
    import pandas as pd
    out = []
    sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    for name, df in sheets.items():
        header = [str(c) for c in df.columns]
        if not (_normalise_columns(header) or {"src_ReltioURI", "match_ReltioURI"} <= set(header)):
            continue
        df = df.fillna("")
        p = os.path.join(tmp, f"{os.path.basename(path)}_{name}.csv")
        _rewrite_csv(df.to_dict("records"), header, default_rule, p)
        out.append(p)
    if not out:
        LOG.add("INPUT_ERROR", "Excel file has no sheet with entity URI columns", source=path, severity="ERROR")
    return out


def read_potential_matches(paths: Sequence[str], default_rule: str = "") -> Iterator[ReportRow]:
    """Yields ReportRow objects. A Rule value of '(infer)' asks the analyser to infer the rule."""
    tmp = tempfile.mkdtemp(prefix="fpnp_pm_")
    for path in paths:
        if not os.path.isfile(path):
            LOG.add("INPUT_ERROR", "Potential Match Report not found", source=path, severity="ERROR")
            continue
        if path.lower().endswith((".xlsx", ".xlsm", ".xls")):
            csvs = _excel_to_csvs(path, default_rule, tmp)
        else:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                rd = csv.DictReader(fh)
                header = rd.fieldnames or []
                if _needs_rewrite(header):
                    p = os.path.join(tmp, os.path.basename(path))
                    _rewrite_csv(rd, list(header), default_rule, p)
                    csvs = [p]
                else:
                    csvs = [path]
        for row in read_reports(csvs):
            if row.source_file != path:
                row.source_file = path
            yield row
