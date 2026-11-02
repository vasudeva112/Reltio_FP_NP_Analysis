"""
Styled, memory-conscious Excel writer for the Reltio FP / FN reports.

  * summary sheets: stacked titled tables (normal openpyxl mode - they are small)
  * detail sheets:  streamed with openpyxl write-only mode, so large pair / attribute sheets do
                    not hold a full worksheet object model in memory; rows above `row_cap` go to
                    the CSV detail file only (sheet title says so)

Rows may be any iterable of dicts (a generator is fine for detail sheets).
"""

import csv
import itertools
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
CELL_LIMIT = 32_000
EXCEL_MAX_ROWS = 1_048_576

NAVY = "1F3A5F"
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
HEADER_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=14, color=NAVY)
SECTION_FONT = Font(bold=True, size=11, color=NAVY)
NOTE_FONT = Font(italic=True, size=9, color="555555")
THIN = Side(style="thin", color="D0D7E2")
BORDER = Border(bottom=THIN)

# fills for classification / band values (light tints so text stays readable when printed)
VALUE_FILLS: Dict[str, str] = {
    "Confirmed False Positive": "F4C7C3", "Potential False Positive": "FCE5CD", "Requires Manual Review": "FFF2CC",
    "Likely Genuine Duplicate": "D9EAD3", "Confirmed Genuine Duplicate": "B6D7A8",
    "Confirmed False Negative": "F4C7C3", "High-confidence potential false negative": "FCE5CD",
    "Medium-confidence potential false negative": "FFF2CC", "Low-confidence candidate": "E8EEF7",
    "Requires manual review": "FFF2CC",
    "EXACT": "D9EAD3", "VARIANT_MATCH": "E2F0D9", "PARTIAL": "FFF2CC", "NO_MATCH": "F4C7C3",
    "BOTH_NULL": "EDEDED", "ONE_NULL": "EDEDED", "MATCH": "D9EAD3",
}
FILL_COLUMNS = {"FPClassification", "FNConfidence", "ComparisonResult", "Classification"}
NUMBER_FORMATS = {"EstimatedSimilarityScore": "0.0", "AttributeScore": "0.0", "Contribution": "0.00",
                  "AnalyticalSimilarity": "0.000", "ComparatorRelevance": "0.000", "PythonRelevance": "0.0000",
                  "ReltioMatchScore": "0.0000", "Share": "0.0%", "Percent": "0.0%"}


def clean(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (list, set, tuple)):
        v = ", ".join(map(str, v))
    if isinstance(v, str):
        v = _ILLEGAL.sub("", v)
        return v if len(v) <= CELL_LIMIT else v[:CELL_LIMIT] + "...[truncated, see CSV]"
    return v


def _columns(rows: Sequence[dict], preferred: Optional[Sequence[str]] = None) -> List[str]:
    cols = list(preferred or [])
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols


def write_csv(path: str, rows: Iterable[dict], columns: Sequence[str]) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore", restval="")
        w.writeheader()
        for r in rows:
            w.writerow({k: clean(v) for k, v in r.items()})
    return path


class ReportWorkbook:
    def __init__(self, path: str, row_cap: int = 200_000):
        self.path = path
        self.row_cap = min(row_cap, EXCEL_MAX_ROWS - 5)
        self.wb = Workbook(write_only=True)
        self.truncations: List[str] = []

    # ------------------------------------------------------------------
    def _header_cells(self, ws, cols):
        cells = []
        for c in cols:
            cell = WriteOnlyCell(ws, value=c)
            cell.font, cell.fill = HEADER_FONT, HEADER_FILL
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cells.append(cell)
        return cells

    def _text(self, ws, value, font=None):
        cell = WriteOnlyCell(ws, value=value)
        if font is not None:
            cell.font = font
        return cell

    def _row_cells(self, ws, cols, r):
        cells = []
        for c in cols:
            v = clean(r.get(c))
            cell = WriteOnlyCell(ws, value=v)
            if c in FILL_COLUMNS and isinstance(v, str) and v in VALUE_FILLS:
                cell.fill = PatternFill("solid", fgColor=VALUE_FILLS[v])
            fmt = NUMBER_FORMATS.get(c)
            if fmt and isinstance(v, (int, float)):
                cell.number_format = fmt
            cells.append(cell)
        return cells

    @staticmethod
    def _widths(ws, cols, sample: Sequence[dict], max_width: int = 60):
        for i, c in enumerate(cols, 1):
            lens = [len(str(c))] + [len(str(r.get(c) if r.get(c) is not None else "")) for r in sample[:200]]
            ws.column_dimensions[get_column_letter(i)].width = max(10, min(max_width, max(lens) + 2))

    # ------------------------------------------------------------------
    def add_sections(self, name: str, title: str, sections: Sequence[tuple], subtitle: str = "") -> None:
        """sections: (section title, rows, optional note). Each section is a small table."""
        ws = self.wb.create_sheet(name[:31])
        all_rows = [r for _, rows, *_ in sections for r in rows]
        width_cols = _columns(all_rows)
        self._widths(ws, width_cols or ["Metric"], all_rows, 70)
        ws.append([self._text(ws, title, TITLE_FONT)])
        if subtitle:
            ws.append([self._text(ws, subtitle, NOTE_FONT)])
        ws.append([])
        for sec in sections:
            sec_title, rows = sec[0], sec[1]
            note = sec[2] if len(sec) > 2 else ""
            ws.append([self._text(ws, sec_title, SECTION_FONT)])
            if note:
                ws.append([self._text(ws, note, NOTE_FONT)])
            if not rows:
                ws.append([self._text(ws, "(no data)", NOTE_FONT)])
                ws.append([])
                continue
            cols = _columns(rows)
            ws.append(self._header_cells(ws, cols))
            for r in rows:
                ws.append(self._row_cells(ws, cols, r))
            ws.append([])

    def add_table(self, name: str, title: str, rows: Iterable[dict], columns: Optional[Sequence[str]] = None,
                  full_file: str = "", note: str = "", sample: Optional[Sequence[dict]] = None) -> int:
        """Streams rows into a sheet. `columns` should be given when `rows` is a generator."""
        ws = self.wb.create_sheet(name[:31])
        rows_iter = iter(rows)
        buffered: List[dict] = []
        if columns is None:
            buffered = list(rows_iter)
            rows_iter = iter(())
            columns = _columns(buffered)
        cols = list(columns) or ["(empty)"]
        self._widths(ws, cols, list(sample or buffered[:200]))
        ws.freeze_panes = "C4" if len(cols) > 2 else "A4"
        heading = title + (f" | Full detail: {os.path.basename(full_file)}" if full_file else "")
        ws.append([self._text(ws, heading, SECTION_FONT)])
        ws.append([self._text(ws, note, NOTE_FONT)] if note else [])
        ws.append(self._header_cells(ws, cols))
        n = 0
        for r in itertools.chain(buffered, rows_iter):
            if n < self.row_cap:
                ws.append(self._row_cells(ws, cols, r))
            n += 1
        if n:
            ws.auto_filter.ref = f"A3:{get_column_letter(len(cols))}{min(n, self.row_cap) + 3}"
        if n > self.row_cap:
            self.truncations.append(f"{name}: first {self.row_cap} of {n} rows in Excel; all rows in {full_file}")
            ws.append([self._text(ws, f"CAPPED: {self.row_cap} of {n} rows shown. Full data: {full_file}",
                                  Font(bold=True, color="C00000"))])
        return n

    def save(self) -> str:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.wb.save(self.path)
        return self.path
