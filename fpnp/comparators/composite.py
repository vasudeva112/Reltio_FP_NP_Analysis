import itertools
import re
from typing import List, Sequence

from ..comparator_registry import create, register
from ..exceptions_log import LOG
from .base import APPROXIMATION, DOCUMENTED, CompareResult, Comparator


def _as_bool(v, default: bool) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("true", "yes", "1")


@register
class RangeNumericComparator(Comparator):
    RELTIO_NAME = "RangeNumericComparator"
    STATUS = APPROXIMATION
    PERMISSIVE = True
    DOC = dict(Comparator.DOC,
               parameters="threshold (numeric tolerance)",
               input_types="number (extracted by the enclosing CustomComparator pattern)",
               normalisation="float() of the value",
               limitations="Equal when |a-b| <= threshold; relevance 1/0. Not described in the "
                           "Reltio documentation that was available.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        try:
            self.threshold = float(self.params.get("threshold", 0))
        except (TypeError, ValueError):
            self.threshold = 0.0

    def compare(self, a: str, b: str) -> CompareResult:
        try:
            x, y = float(a), float(b)
        except ValueError:
            LOG.add("FAILED_COMPARISON", "RangeNumericComparator got a non-numeric value",
                    group=self.group, attribute=self.attribute, example=f"{a!r} vs {b!r}")
            return CompareResult(False, 0.0, "non-numeric")
        eq = abs(x - y) <= self.threshold
        return CompareResult(eq, 1.0 if eq else 0.0, f"|{x:g}-{y:g}|={abs(x - y):g} <= {self.threshold:g}")


@register
class CustomComparator(Comparator):
    RELTIO_NAME = "CustomComparator"
    STATUS = APPROXIMATION
    PERMISSIVE = True
    DOC = dict(Comparator.DOC,
               parameters="groups[]: className, classParams, pattern, noiseDictionary, acceptMissingBoth, "
                          "acceptMissingOneOf, useNoiseIfEmpty",
               normalisation="Per group: regex pattern extracts the part to compare (capture group 1 if "
                             "present), noise words removed when the dictionary is available locally.",
               limitations="Result is the AND of all groups (documented); relevance = minimum of group "
                           "relevances (assumption). Remote/built-in noise dictionaries are not available. "
                           "transliterate, wordReplacements, stemmer and soundex options are not applied.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.groups = []
        for g in self.params.get("groups") or []:
            params = dict(g.get("classParams") or {})
            sub = create(g.get("className") or "BasicStringComparator", params, self.ctx, self.group,
                         self.attribute)
            pattern = re.compile(g["pattern"]) if g.get("pattern") else None
            noise = None
            nd = g.get("noiseDictionary")
            if nd:
                noise = self.ctx.noise_dictionaries.get(nd)
                if noise is None:
                    LOG.add("APPROXIMATION", f"Noise dictionary not available locally: {nd}",
                            group=self.group, attribute=self.attribute)
            for opt in ("transliterate", "wordReplacements", "useStemmer", "useSoundex", "splitByWordsBoundaries"):
                if opt in g or opt in params:
                    LOG.add("UNSUPPORTED_PARAMETER", f"CustomComparator option '{opt}' not applied",
                            group=self.group, attribute=self.attribute)
            self.groups.append({
                "sub": sub, "pattern": pattern, "noise": noise,
                "both": _as_bool(g.get("acceptMissingBoth"), False),
                "one": _as_bool(g.get("acceptMissingOneOf"), False),
            })
        if not self.groups:
            self.groups.append({"sub": create("BasicStringComparator", {}, self.ctx), "pattern": None,
                                "noise": None, "both": False, "one": False})

    @property
    def status(self) -> str:
        return APPROXIMATION

    def _extract(self, g, s: str):
        if g["pattern"] is not None:
            m = g["pattern"].search(s)
            if not m:
                return None
            s = m.group(1) if m.groups() else m.group(0)
        if g["noise"]:
            words = [w for w in re.findall(r"\w+", s.lower()) if w not in g["noise"]]
            s = " ".join(words) if words else s
        return s

    def compare(self, a: str, b: str) -> CompareResult:
        rel = 1.0
        details = []
        for g in self.groups:
            xa, xb = self._extract(g, a), self._extract(g, b)
            if xa is None and xb is None:
                ok = g["both"]
                r = CompareResult(ok, 1.0 if ok else 0.0, "pattern missing both")
            elif xa is None or xb is None:
                ok = g["one"]
                r = CompareResult(ok, 1.0 if ok else 0.0, "pattern missing one side")
            else:
                r = g["sub"].compare(xa, xb)
            details.append(f"{g['sub'].RELTIO_NAME}({xa}~{xb})={'T' if r.equal else 'F'}")
            if not r.equal:
                return CompareResult(False, min(rel, r.relevance), "; ".join(details))
            rel = min(rel, r.relevance)
        return CompareResult(True, rel, "; ".join(details))


@register
class CrossMultiComparator(Comparator):
    """
    Used by the `multi` operator. compare_groups() receives, for each entity, one value
    list per attribute of the multi group, and the per-attribute comparators.
    """
    RELTIO_NAME = "CrossMultiComparator"
    STATUS = DOCUMENTED
    DEFAULT_TOKEN_CLASS = "com.reltio.match.token.CrossMultiToken"
    PERMISSIVE = True
    DOC = dict(Comparator.DOC,
               parameters="none",
               input_types="group of attributes (multi operator)",
               normalisation="Every assignment of left attributes to right attributes is tried.",
               multi_value="Within one attribute the best value pair is used.",
               limitations="Relevance per permutation = mean of attribute relevances; best permutation "
                           "wins (documented). Equal when every attribute in the best permutation is equal.")

    def compare(self, a: str, b: str) -> CompareResult:
        eq = self.ctx.norm(a) == self.ctx.norm(b)
        return CompareResult(eq, 1.0 if eq else 0.0)

    def compare_groups(self, left: Sequence[List[str]], right: Sequence[List[str]],
                       comparators: Sequence[Comparator]) -> CompareResult:
        n = len(left)
        best = CompareResult(False, 0.0, "no values")
        for perm in itertools.permutations(range(n)):
            rels, eqs, parts = [], [], []
            for i, j in enumerate(perm):
                cmp = comparators[i]
                r_best = CompareResult(False, 0.0)
                for lv in left[i]:
                    for rv in right[j]:
                        r = cmp.compare(lv, rv)
                        if (r.equal, r.relevance) > (r_best.equal, r_best.relevance):
                            r_best = r
                rels.append(r_best.relevance)
                eqs.append(r_best.equal)
                parts.append(f"L{i}->R{j}:{'T' if r_best.equal else 'F'}")
            res = CompareResult(all(eqs) and bool(eqs), sum(rels) / n if n else 0.0, ", ".join(parts))
            if (res.equal, res.relevance) > (best.equal, best.relevance):
                best = res
        return best
