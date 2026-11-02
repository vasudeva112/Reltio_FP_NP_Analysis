"""
Shows a value the way the configured comparator sees it, and computes the analytical
(comparator-independent) closeness of two values.

  comparator_view()   what the Reltio comparator compares - only the normalisation the comparator
                      class itself applies (no extra punctuation stripping etc. unless configured)
  analytical_similarity()  0..1 closeness used for scoring near misses; typed:
                      dates  -> component agreement (year / month / day, day-month swap)
                      digits -> 1 - edit distance / length on the digit string (identifiers, phones)
                      text   -> best of Jaro-Winkler, token-sort Jaro-Winkler and word overlap
"""

import re
from datetime import date, datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

import jellyfish

from ..comparators.address_phone import ADDRESS_NOISE, address_words, strip_noise
from ..comparators.base import Comparator, EngineContext
from ..comparators.phonetic import double_metaphone

_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"), "ymd"),
    (re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})"), "ymd"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "mdy"),
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), "ymd"),
)
MAX_VIEW = 300


def parse_date(v: str) -> Optional[Tuple[int, int, int]]:
    s = (v or "").strip()
    for pat, order in _DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        y, mo, d = (a, b, c) if order == "ymd" else (c, a, b)
        try:
            date(y, mo, d)
        except ValueError:
            continue
        return y, mo, d
    if re.fullmatch(r"-?\d{12,13}", s):                    # epoch milliseconds (Reltio timestamps)
        try:
            dt = datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
            return dt.year, dt.month, dt.day
        except (OverflowError, OSError, ValueError):
            return None
    return None


def digits_only(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def is_digit_like(v: str) -> bool:
    """Mostly digits (identifiers, phone numbers): >= 80% of the alphanumeric characters."""
    alnum = re.sub(r"[^0-9A-Za-z]", "", v or "")
    return len(alnum) >= 3 and sum(ch.isdigit() for ch in alnum) / len(alnum) >= 0.8


def _date_similarity(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> float:
    if a == b:
        return 1.0
    if (a[0], a[1], a[2]) == (b[0], b[2], b[1]):         # day / month transposed
        return 0.85
    try:
        delta = abs((date(*a) - date(*b)).days)
    except ValueError:
        delta = 9999
    if delta <= 1:
        return 0.9
    same = sum(x == y for x, y in zip(a, b))
    return {2: 0.75, 1: 0.4}.get(same, 0.0)


def _digit_similarity(a: str, b: str) -> float:
    da, db = digits_only(a), digits_only(b)
    if not da or not db:
        return 0.0
    if da == db:
        return 1.0
    if len(da) >= 7 and len(db) >= 7 and (da.endswith(db) or db.endswith(da)):
        return 0.9                                        # country code / prefix difference
    d = jellyfish.damerau_levenshtein_distance(da, db)
    return max(0.0, 1.0 - d / max(len(da), len(db)))


def _words(s: str) -> List[str]:
    return re.findall(r"\w+", s.lower())


def _text_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    jw = jellyfish.jaro_winkler_similarity(a, b)
    wa, wb = _words(a), _words(b)
    sort_jw = jellyfish.jaro_winkler_similarity(" ".join(sorted(wa)), " ".join(sorted(wb))) if wa and wb else 0.0
    sa, sb = set(wa), set(wb)
    jac = len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    return max(jw, sort_jw, jac)


def value_kind(a: str, b: str) -> str:
    if parse_date(a) and parse_date(b):
        return "date"
    if is_digit_like(a) and is_digit_like(b):
        return "digits"
    return "text"


def analytical_similarity(a: str, b: str, ctx: EngineContext) -> Tuple[float, str]:
    """(similarity 0..1, kind) of two single values."""
    na, nb = ctx.norm(a), ctx.norm(b)
    kind = value_kind(na, nb)
    if kind == "date":
        return _date_similarity(parse_date(na), parse_date(nb)), kind
    if kind == "digits":
        return _digit_similarity(na, nb), kind
    return _text_similarity(na, nb), kind


def best_similarity(left: Sequence[str], right: Sequence[str], ctx: EngineContext,
                    limit: int = 25) -> Tuple[Optional[float], str, Tuple[str, str]]:
    """Best analytical similarity over multi-value pairs (at most `limit` values per side)."""
    best, kind, pair = None, "", ("", "")
    for a in list(left)[:limit]:
        for b in list(right)[:limit]:
            s, k = analytical_similarity(a, b, ctx)
            if best is None or s > best:
                best, kind, pair = s, k, (a, b)
    return best, kind, pair


# ----------------------------------------------------------------------
def comparator_view(cmp: Comparator, value: str, ctx: EngineContext) -> str:
    """The value as the comparator compares it."""
    name = getattr(cmp, "RELTIO_NAME", "")
    n = ctx.norm(value)
    if name == "DoubleMetaphoneComparator":
        codes = double_metaphone(n)
        return f"{n} [DM {'/'.join(codes) or '-'}]"
    if name == "SoundexComparator":
        letters = "".join(ch for ch in n if ch.isalpha())
        return f"{n} [Soundex {jellyfish.soundex(letters) if letters else '-'}]"
    if name == "MetaphoneComparator":
        return f"{n} [Metaphone {jellyfish.metaphone(n) or '-'}]"
    if name == "PhoneNumberComparator":
        d = digits_only(value)
        return d[-10:] if len(d) >= 10 else f"{d} (<10 digits)"
    if name == "AddressLineComparator":
        return " ".join(strip_noise(address_words(value), ADDRESS_NOISE))
    if name == "RangeNumericComparator":
        return n
    if name == "CustomComparator" and getattr(cmp, "groups", None):
        parts = []
        for g in cmp.groups:
            x = cmp._extract(g, value)
            parts.append(f"{g['sub'].RELTIO_NAME}:{'-' if x is None else x}")
        return "; ".join(parts)
    if getattr(cmp, "STATUS", "") == "Unsupported":
        return f"{n} (comparator not reproduced)"
    return n


def view_list(cmp: Comparator, values: Iterable[str], ctx: EngineContext) -> str:
    out = list(dict.fromkeys(comparator_view(cmp, v, ctx) for v in values))
    s = " | ".join(out)
    return s if len(s) <= MAX_VIEW else s[:MAX_VIEW] + " ..."
