"""
Workbook + detail files.

Excel is the investigation interface; full pair-level detail is always written to CSV
(and Parquet when pyarrow is installed). A sheet whose data exceeds EXCEL_ROW_CAP shows
a clearly labelled capped extract and points to the full file.
"""

import csv
import logging
import os
import re
from typing import Dict, List, Optional, Sequence

import pandas as pd
from openpyxl.styles import Font

log = logging.getLogger("fpnp")

EXCEL_MAX_ROWS = 1_048_576
CELL_LIMIT = 32_000
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(v):
    if isinstance(v, str):
        v = _ILLEGAL.sub("", v)
        return v if len(v) <= CELL_LIMIT else v[:CELL_LIMIT] + "...[truncated, see CSV]"
    if isinstance(v, (list, set, tuple)):
        return _clean(", ".join(map(str, v)))
    return v


def _columns(rows: Sequence[dict], preferred: Optional[List[str]] = None) -> List[str]:
    cols: List[str] = list(preferred or [])
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols


def write_csv(path: str, rows: Sequence[dict], columns: Optional[List[str]] = None) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = _columns(rows, columns)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore", restval="")
        w.writeheader()
        for r in rows:
            w.writerow({k: (", ".join(map(str, v)) if isinstance(v, (list, set, tuple)) else v)
                        for k, v in r.items()})
    return path


def write_parquet(path: str, rows: Sequence[dict]) -> Optional[str]:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        return None
    if not rows:
        return None
    df = pd.DataFrame([{k: (None if v is None else str(v)) for k, v in r.items()} for r in rows])
    df.to_parquet(path, index=False)
    return path


class Workbook:
    def __init__(self, path: str, row_cap: int):
        self.path = path
        self.row_cap = min(row_cap, EXCEL_MAX_ROWS - 3)
        self.sheets: List[tuple] = []
        self.truncations: List[str] = []

    def add(self, name: str, rows: Sequence[dict], full_file: str = "", columns: Optional[List[str]] = None,
            drop: Sequence[str] = ("TraceJSON",), note: str = "", cap: bool = True) -> None:
        total = len(rows)
        shown = rows[: self.row_cap] if cap else rows
        title = f"{name}: {total} row(s)"
        if total > len(shown):
            title = (f"{name}: CAPPED EXTRACT - showing {len(shown)} of {total} rows. "
                     f"Full data: {full_file}")
            self.truncations.append(f"{name}: {len(shown)} of {total} rows shown; full data in {full_file}")
        elif full_file:
            title += f". Full detail (incl. JSON traces): {full_file}"
        if note:
            title += f". {note}"
        cols = [c for c in _columns(shown, columns) if c not in drop]
        self.sheets.append((name, title, shown, cols))

    def save(self) -> str:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with pd.ExcelWriter(self.path, engine="openpyxl") as xw:
            for name, title, rows, cols in self.sheets:
                df = pd.DataFrame([{c: _clean(r.get(c)) for c in cols} for r in rows], columns=cols or ["(empty)"])
                df.to_excel(xw, sheet_name=name[:31], index=False, startrow=1)
                ws = xw.sheets[name[:31]]
                ws["A1"] = title
                ws["A1"].font = Font(bold=True)
                ws.freeze_panes = "B3" if len(cols) > 1 else "A3"
                if rows:
                    ws.auto_filter.ref = f"A2:{ws.cell(row=2, column=max(1, len(cols))).column_letter}{len(rows) + 2}"
                for i, c in enumerate(cols or ["(empty)"], 1):
                    letter = ws.cell(row=2, column=i).column_letter
                    sample = [len(str(c))] + [len(str(r.get(c) or "")) for r in rows[:200]]
                    ws.column_dimensions[letter].width = max(10, min(60, max(sample) + 2))
                    ws.cell(row=2, column=i).font = Font(bold=True)
        return self.path


def summary_row(section: str, metric: str, value, basis: str = "", note: str = "") -> dict:
    return {"Section": section, "Metric": metric, "Value": value, "Basis": basis, "Note": note}
