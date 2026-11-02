"""Deterministic pair keys and pair-level aggregation of rule-level results."""

from collections import OrderedDict
from typing import Dict, Iterable, List, Tuple

PAIR_SEP = "||"


def normalise_uri(uri: str) -> str:
    """'0BB2d7v', 'entities/0BB2d7v', full https URL -> 'entities/0BB2d7v'."""
    u = (uri or "").strip()
    if "/entities/" in u:
        u = "entities/" + u.split("/entities/", 1)[1]
    elif u and not u.startswith("entities/"):
        u = f"entities/{u}"
    return u.split("?", 1)[0].rstrip("/")


def pair_key(a: str, b: str) -> str:
    x, y = sorted((normalise_uri(a), normalise_uri(b)))
    return f"{x}{PAIR_SEP}{y}"


def split_key(key: str) -> Tuple[str, str]:
    a, b = key.split(PAIR_SEP, 1)
    return a, b


def aggregate(rows: Iterable[dict], key_field: str = "PairKey") -> "OrderedDict[str, List[dict]]":
    """Groups rule-level rows by pair key, keeping every rule-level row."""
    out: "OrderedDict[str, List[dict]]" = OrderedDict()
    for r in rows:
        out.setdefault(r[key_field], []).append(r)
    return out


class PairRegistry:
    """Pairs already seen, per rule and overall (never evaluate the same pair twice)."""

    def __init__(self):
        self.by_pair: Dict[str, set] = {}

    def add(self, a: str, b: str, group_uri: str) -> str:
        k = pair_key(a, b)
        self.by_pair.setdefault(k, set()).add(group_uri)
        return k

    def groups(self, key: str) -> set:
        return self.by_pair.get(key, set())

    def __contains__(self, key: str) -> bool:
        return key in self.by_pair

    def __len__(self) -> int:
        return len(self.by_pair)
