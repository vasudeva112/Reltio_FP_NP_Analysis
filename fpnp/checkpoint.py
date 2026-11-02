"""
SQLite checkpoint store: persists evaluated pair keys and rule-level results so an
interrupted FN run resumes without evaluating any pair twice.
"""

import json
import os
import sqlite3
import threading
from typing import Iterable, Iterator, List, Optional, Set, Tuple


class Checkpoint:
    def __init__(self, path: str, run_signature: str, resume: bool = True):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not resume and os.path.exists(path):
            os.remove(path)
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS evaluated (pair_key TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS results (pair_key TEXT, group_uri TEXT, data TEXT, "
                        "PRIMARY KEY (pair_key, group_uri))")
        self.db.execute("CREATE TABLE IF NOT EXISTS stats (k TEXT PRIMARY KEY, v TEXT)")
        old = self.get_meta("signature")
        if old and old != run_signature:
            # configuration / data / parameters changed: a resume would mix results
            self.db.executescript("DELETE FROM evaluated; DELETE FROM results; DELETE FROM stats;")
        self.set_meta("signature", run_signature)
        self.db.commit()

    def get_meta(self, k: str) -> Optional[str]:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k: str, v: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
            self.db.commit()

    def evaluated_keys(self) -> Set[str]:
        return {r[0] for r in self.db.execute("SELECT pair_key FROM evaluated")}

    def save_batch(self, pair_keys: Iterable[str], results: Iterable[Tuple[str, str, dict]]) -> None:
        with self.lock:
            self.db.executemany("INSERT OR IGNORE INTO evaluated VALUES (?)", ((k,) for k in pair_keys))
            self.db.executemany("INSERT OR REPLACE INTO results VALUES (?,?,?)",
                                ((k, g, json.dumps(d, default=str)) for k, g, d in results))
            self.db.commit()

    def results(self) -> Iterator[Tuple[str, str, dict]]:
        for k, g, d in self.db.execute("SELECT pair_key, group_uri, data FROM results ORDER BY pair_key, group_uri"):
            yield k, g, json.loads(d)

    def save_stats(self, name: str, obj) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO stats VALUES (?,?)", (name, json.dumps(obj, default=str)))
            self.db.commit()

    def load_stats(self, name: str):
        row = self.db.execute("SELECT v FROM stats WHERE k=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        self.db.close()
