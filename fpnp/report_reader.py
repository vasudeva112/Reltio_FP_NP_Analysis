"""
Reads Potential Match Report files written by Potential_Matches_Extract_v3 (and the
older v1/v2 layouts): Rule, [RuleLabel], src_/match_ ReltioURI, [Crosswalks], and one
src_<dotted path> / match_<dotted path> column pair per rule attribute.
Columns are mapped by name to attribute URIs - never by position.
"""

import csv
from dataclasses import dataclass, field
from typing import Dict, Iterator, List

from .exceptions_log import LOG
from .pair_deduplication import normalise_uri, pair_key
from .rule_config_parser import entity_type_of

FIXED = {"ReltioURI", "Crosswalks"}


@dataclass
class ReportRow:
    rule_uri: str
    rule_label: str
    src_uri: str
    match_uri: str
    src_crosswalks: str
    match_crosswalks: str
    src_values: Dict[str, str]
    match_values: Dict[str, str]
    source_file: str
    row_no: int
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def entity_type(self) -> str:
        return entity_type_of(self.rule_uri)

    @property
    def key(self) -> str:
        return pair_key(self.src_uri, self.match_uri)


def read_reports(paths: List[str]) -> Iterator[ReportRow]:
    for path in paths:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            cols = rd.fieldnames or []
            if "Rule" not in cols or "src_ReltioURI" not in cols or "match_ReltioURI" not in cols:
                LOG.add("INPUT_ERROR", "Not a Potential Match Report (Rule/src_ReltioURI/match_ReltioURI missing)",
                        source=path, severity="ERROR")
                continue
            attrs = [c[4:] for c in cols if c.startswith("src_") and c[4:] not in FIXED]
            unpaired = [a for a in attrs if f"match_{a}" not in cols]
            for a in unpaired:
                LOG.add("INPUT_ERROR", "src_ column without matching match_ column", source=path, attribute=a)
            attrs = [a for a in attrs if a not in unpaired]
            other = [c for c in cols if not c.startswith(("src_", "match_")) and c not in ("Rule", "RuleLabel")]
            for n, row in enumerate(rd, 2):
                src, mat = row.get("src_ReltioURI", ""), row.get("match_ReltioURI", "")
                if not src or not mat or not row.get("Rule"):
                    LOG.add("INPUT_ERROR", "Report row without rule or entity URI skipped", source=f"{path}:{n}")
                    continue
                yield ReportRow(
                    rule_uri=row["Rule"].strip(),
                    rule_label=row.get("RuleLabel", "") or "",
                    src_uri=normalise_uri(src), match_uri=normalise_uri(mat),
                    src_crosswalks=row.get("src_Crosswalks", "") or "",
                    match_crosswalks=row.get("match_Crosswalks", "") or "",
                    src_values={a: row.get(f"src_{a}", "") or "" for a in attrs},
                    match_values={a: row.get(f"match_{a}", "") or "" for a in attrs},
                    source_file=path, row_no=n,
                    extra={c: row.get(c, "") for c in other},
                )


def report_pair_index(rows: List[ReportRow]) -> Dict[str, set]:
    """pair key -> set of rule URIs Reltio returned for the pair."""
    idx: Dict[str, set] = {}
    for r in rows:
        idx.setdefault(r.key, set()).add(r.rule_uri)
    return idx
