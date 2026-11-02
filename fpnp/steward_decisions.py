"""
Optional steward ground truth.

CSV with one row per reviewed pair. Column names are matched case-insensitively:
  entity 1 : EntityURI1 | Entity1 | entity_uri_1 | SourceURI | src_ReltioURI
  entity 2 : EntityURI2 | Entity2 | entity_uri_2 | MatchedURI | match_ReltioURI
  decision : Decision | StewardDecision | Label | Review
  optional : Reviewer, DecisionDate, Comment

Decision values:
  NOT_A_MATCH, NOTMATCH, NOT MATCH, DIFFERENT, NO, FALSE_POSITIVE  -> NOT_A_MATCH
  MATCH, AS_MATCH, SAME, YES, TRUE_POSITIVE                        -> MATCH
  MERGED, MERGE, MANUAL_MERGE                                      -> MERGED
anything else is recorded as an exception and ignored.
"""

import csv
import os
from dataclasses import dataclass
from typing import Dict, Optional

from .exceptions_log import LOG
from .pair_deduplication import pair_key

NOT_A_MATCH = "NOT_A_MATCH"
MATCH = "MATCH"
MERGED = "MERGED"

_E1 = ("entityuri1", "entity1", "entity_uri_1", "sourceuri", "src_reltiouri", "uri1")
_E2 = ("entityuri2", "entity2", "entity_uri_2", "matcheduri", "match_reltiouri", "uri2")
_DEC = ("decision", "stewarddecision", "label", "review")
_MAP = {
    "not_a_match": NOT_A_MATCH, "notmatch": NOT_A_MATCH, "not match": NOT_A_MATCH, "not_match": NOT_A_MATCH,
    "different": NOT_A_MATCH, "no": NOT_A_MATCH, "false_positive": NOT_A_MATCH, "notamatch": NOT_A_MATCH,
    "match": MATCH, "as_match": MATCH, "same": MATCH, "yes": MATCH, "true_positive": MATCH,
    "merged": MERGED, "merge": MERGED, "manual_merge": MERGED,
}


@dataclass
class Decision:
    kind: str
    raw: str
    source: str
    reviewer: str = ""
    date: str = ""
    comment: str = ""

    @property
    def same_entity(self) -> bool:
        return self.kind in (MATCH, MERGED)


def _col(fieldnames, options) -> Optional[str]:
    low = {f.lower().strip(): f for f in fieldnames or []}
    for o in options:
        if o in low:
            return low[o]
    return None


def load(path: str) -> Dict[str, Decision]:
    out: Dict[str, Decision] = {}
    if not path:
        return out
    if not os.path.isfile(path):
        LOG.add("INPUT_ERROR", "Steward decisions file not found", source=path, severity="ERROR")
        return out
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        c1, c2, cd = _col(rd.fieldnames, _E1), _col(rd.fieldnames, _E2), _col(rd.fieldnames, _DEC)
        if not (c1 and c2 and cd):
            LOG.add("INPUT_ERROR", "Steward decisions file lacks entity/decision columns", source=path,
                    example=str(rd.fieldnames), severity="ERROR")
            return out
        cr, cdt, cc = _col(rd.fieldnames, ("reviewer",)), _col(rd.fieldnames, ("decisiondate", "date")), \
            _col(rd.fieldnames, ("comment", "comments"))
        for n, row in enumerate(rd, 2):
            raw = (row.get(cd) or "").strip()
            kind = _MAP.get(raw.lower().replace("-", "_"))
            if not kind:
                if raw:
                    LOG.add("INPUT_ERROR", "Unrecognised steward decision value", source=f"{path}:{n}", example=raw)
                continue
            k = pair_key(row[c1], row[c2])
            prev = out.get(k)
            if prev and prev.same_entity != (kind in (MATCH, MERGED)):
                LOG.add("CONFLICTING_GROUND_TRUTH", "Conflicting steward decisions for the same pair - "
                                                    "pair treated as unlabelled", source=f"{path}:{n}", example=k)
                out[k] = Decision("CONFLICT", f"{prev.raw}/{raw}", path)
                continue
            out[k] = Decision(kind, raw, path, row.get(cr, "") if cr else "", row.get(cdt, "") if cdt else "",
                              row.get(cc, "") if cc else "")
    return out
