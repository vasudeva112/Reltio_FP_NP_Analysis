from functools import lru_cache
from typing import Tuple

from ..comparator_registry import register
from ..exceptions_log import LOG
from .base import APPROXIMATION, DOCUMENTED, CompareResult, Comparator

try:
    from metaphone import doublemetaphone as _dm
    HAS_DOUBLE_METAPHONE = True
except ImportError:                     # pragma: no cover - depends on environment
    import jellyfish
    HAS_DOUBLE_METAPHONE = False

    def _dm(s: str) -> Tuple[str, str]:
        return jellyfish.metaphone(s), ""


# Code length: the original Double Metaphone algorithm and Apache Commons Codec (default maxCodeLen)
# keep 4 characters; the 'metaphone' package returns unlimited codes. 0 = unlimited.
DM_MAX_LENGTH = 4


def configure(max_length: int) -> None:
    global DM_MAX_LENGTH
    if max_length != DM_MAX_LENGTH:
        DM_MAX_LENGTH = max_length
        double_metaphone.cache_clear()


@lru_cache(maxsize=200_000)
def double_metaphone(s: str) -> Tuple[str, ...]:
    """Primary and secondary codes (truncated to DM_MAX_LENGTH), empty codes removed."""
    if not HAS_DOUBLE_METAPHONE:
        LOG.add("APPROXIMATION", "Package 'metaphone' missing - single Metaphone used instead of "
                                 "Double Metaphone (pip install metaphone)")
    codes = _dm(s or "")
    if DM_MAX_LENGTH > 0:
        codes = tuple(c[:DM_MAX_LENGTH] for c in codes)
    return tuple(c for c in dict.fromkeys(codes) if c)


@register
class DoubleMetaphoneComparator(Comparator):
    RELTIO_NAME = "DoubleMetaphoneComparator"
    STATUS = DOCUMENTED if HAS_DOUBLE_METAPHONE else APPROXIMATION
    DEFAULT_TOKEN_CLASS = "com.reltio.match.token.DoubleMetaphoneMatchToken"
    PERMISSIVE = True
    DOC = dict(Comparator.DOC,
               parameters="none",
               case_whitespace="Algorithm is case-insensitive; surrounding whitespace trimmed.",
               normalisation="Double Metaphone primary + secondary code of the whole value, truncated to "
                             "DOUBLE_METAPHONE_MAX_LENGTH characters (default 4 as in the original algorithm and "
                             "Apache Commons Codec).",
               limitations="Equal when the code sets intersect (primary/secondary cross match is an "
                           "assumption). Values without letters fall back to normalised equality. "
                           "Non-Latin scripts unsupported (as in Reltio).")

    def compare(self, a: str, b: str) -> CompareResult:
        ca, cb = double_metaphone(self.ctx.norm(a)), double_metaphone(self.ctx.norm(b))
        if not ca or not cb:
            eq = self.ctx.norm(a) == self.ctx.norm(b)
            return CompareResult(eq, 1.0 if eq else 0.0, "no phonetic code; string equality")
        eq = bool(set(ca) & set(cb))
        return CompareResult(eq, 1.0 if eq else 0.0, f"{'/'.join(ca)} vs {'/'.join(cb)}")
