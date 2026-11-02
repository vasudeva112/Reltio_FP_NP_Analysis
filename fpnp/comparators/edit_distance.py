import jellyfish

from ..comparator_registry import register
from ..exceptions_log import LOG
from .base import APPROXIMATION, DOCUMENTED, CompareResult, Comparator

# Reltio default table for DynamicDamerauLevenshteinDistance: (max shortest length, max edits)
DYNAMIC_DL_TABLE = ((6, 1), (10, 2), (20, 3), (30, 4))
DYNAMIC_DL_MAX = 5


def dynamic_allowed_distance(shortest_len: int) -> int:
    for limit, edits in DYNAMIC_DL_TABLE:
        if shortest_len <= limit:
            return edits
    return DYNAMIC_DL_MAX


@register
class DynamicDamerauLevenshteinDistance(Comparator):
    RELTIO_NAME = "DynamicDamerauLevenshteinDistance"
    STATUS = APPROXIMATION
    PERMISSIVE = True
    DOC = dict(Comparator.DOC,
               parameters="thresholds (by length or percentage) - not used by this tenant; default table applies",
               case_whitespace="Case-insensitive by default, trimmed.",
               normalisation="Damerau-Levenshtein distance on normalised strings.",
               limitations="Allowed edits by shortest raw length: <=6:1, <=10:2, <=20:3, <=30:4, >30:5 "
                           "(documented). Relevance 1 - d^2/maxlen^2 (documented). Reltio says its "
                           "internal calculation differs slightly; documentation wording on the fuzzy "
                           "operator is ambiguous.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if self.params.get("thresholds"):
            LOG.add("UNSUPPORTED_PARAMETER", "Custom 'thresholds' ignored; default distance table used",
                    group=self.group, attribute=self.attribute)

    def compare(self, a: str, b: str) -> CompareResult:
        x, y = self.ctx.norm(a), self.ctx.norm(b)
        d = jellyfish.damerau_levenshtein_distance(x, y)
        allowed = dynamic_allowed_distance(min(len(a.strip()), len(b.strip())))
        m = max(len(x), len(y)) or 1
        rel = max(0.0, 1.0 - (d * d) / (m * m))
        return CompareResult(d <= allowed, rel, f"distance {d}, allowed {allowed}")


@register
class DamerauLevenshteinDistance(DynamicDamerauLevenshteinDistance):
    RELTIO_NAME = "DamerauLevenshteinDistance"
    STATUS = APPROXIMATION
    DOC = dict(DynamicDamerauLevenshteinDistance.DOC,
               limitations="Documented: shortest length <=4 -> 1 edit, 7-10 -> 2; lengths 5-6 and >10 "
                           "are undocumented and use the dynamic table. Relevance 1 - d/maxlen.")

    def compare(self, a: str, b: str) -> CompareResult:
        x, y = self.ctx.norm(a), self.ctx.norm(b)
        d = jellyfish.damerau_levenshtein_distance(x, y)
        allowed = dynamic_allowed_distance(min(len(a.strip()), len(b.strip())))
        m = max(len(x), len(y)) or 1
        return CompareResult(d <= allowed, max(0.0, 1.0 - d / m), f"distance {d}, allowed {allowed}")


@register
class JaroWinklerComparator(Comparator):
    RELTIO_NAME = "JaroWinklerComparator"
    STATUS = DOCUMENTED
    PERMISSIVE = True
    DOC = dict(Comparator.DOC,
               parameters="similarityThreshold (default 0.9)",
               case_whitespace="Case-insensitive by default, trimmed.",
               normalisation="Jaro-Winkler similarity (prefix scale 0.1, max prefix 4) as in Apache Commons.",
               limitations="Relevance = similarity (assumption: docs do not describe relevance).")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        try:
            self.threshold = float(self.params.get("similarityThreshold", 0.9))
        except (TypeError, ValueError):
            self.threshold = 0.9
            LOG.add("INVALID_PARAMETER", "similarityThreshold not numeric - 0.9 used",
                    group=self.group, attribute=self.attribute)

    def compare(self, a: str, b: str) -> CompareResult:
        sim = jellyfish.jaro_winkler_similarity(self.ctx.norm(a), self.ctx.norm(b))
        return CompareResult(sim >= self.threshold, sim, f"similarity {sim:.4f} vs {self.threshold}")
