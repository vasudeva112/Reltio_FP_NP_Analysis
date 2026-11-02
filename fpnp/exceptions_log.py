"""Processing Exceptions collector (Sheet 7). Nothing is skipped silently."""

import threading
from collections import Counter
from typing import Dict, List, Optional


class ExceptionLog:
    """Keeps every distinct exception once, plus an occurrence count."""

    def __init__(self, max_examples: int = 5):
        self._rows: Dict[tuple, dict] = {}
        self._lock = threading.Lock()
        self.max_examples = max_examples
        self.counts: Counter = Counter()

    def add(self, category: str, message: str, *, group: str = "", attribute: str = "",
            source: str = "", example: Optional[str] = None, severity: str = "WARNING") -> None:
        key = (category, message, group, attribute, source)
        with self._lock:
            self.counts[category] += 1
            row = self._rows.get(key)
            if row is None:
                row = {"Severity": severity, "Category": category, "Message": message,
                       "MatchGroup": group, "Attribute": attribute, "Source": source,
                       "Occurrences": 0, "Examples": []}
                self._rows[key] = row
            row["Occurrences"] += 1
            if example and len(row["Examples"]) < self.max_examples:
                row["Examples"].append(str(example)[:300])

    def rows(self) -> List[dict]:
        out = []
        for r in self._rows.values():
            r2 = dict(r)
            r2["Examples"] = " || ".join(r["Examples"])
            out.append(r2)
        sev = {"ERROR": 0, "WARNING": 1, "INFO": 2}
        return sorted(out, key=lambda r: (sev.get(r["Severity"], 3), r["Category"], r["MatchGroup"]))

    def snapshot(self) -> Dict[tuple, dict]:
        """Raw rows, used to ship exceptions back from worker processes."""
        with self._lock:
            return {k: dict(v, Examples=list(v["Examples"])) for k, v in self._rows.items()}

    def merge_snapshot(self, snap: Dict[tuple, dict]) -> None:
        with self._lock:
            for key, r in snap.items():
                self.counts[r["Category"]] += r["Occurrences"]
                mine = self._rows.get(key)
                if mine is None:
                    self._rows[key] = dict(r, Examples=list(r["Examples"]))
                    continue
                mine["Occurrences"] += r["Occurrences"]
                room = self.max_examples - len(mine["Examples"])
                if room > 0:
                    mine["Examples"].extend(r["Examples"][:room])

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()
            self.counts.clear()


# One process-wide log; modules write into it, the report reads it.
LOG = ExceptionLog()
