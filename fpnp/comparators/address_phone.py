import re

from ..comparator_registry import register
from .base import APPROXIMATION, DOCUMENTED, CompareResult, Comparator

# Reltio's addressLine noise dictionary is not published; this list is an approximation.
ADDRESS_NOISE = {
    "st", "street", "ave", "av", "avenue", "rd", "road", "dr", "drive", "ln", "lane", "blvd",
    "boulevard", "ct", "court", "pl", "place", "way", "ter", "terrace", "cir", "circle", "pkwy",
    "parkway", "hwy", "highway", "n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south",
    "east", "west", "apt", "unit", "ste", "suite", "fl", "floor",
}
_PO_BOX = re.compile(r"^p\s*\.?\s*o\.?\s*box\s*(\w+)", re.I)


def address_words(s: str) -> list:
    return re.findall(r"[a-z0-9]+", s.lower())


def strip_noise(words: list, noise: set) -> list:
    kept = [w for w in words if w not in noise]
    return kept or words


@register
class AddressLineComparator(Comparator):
    RELTIO_NAME = "AddressLineComparator"
    STATUS = APPROXIMATION
    DEFAULT_TOKEN_CLASS = "com.reltio.match.token.AddressLineMatchToken"
    PERMISSIVE = True
    DOC = dict(Comparator.DOC,
               parameters="none",
               case_whitespace="Lower-cased, letters and digits only.",
               normalisation="PO Box values compared on the box number; otherwise words minus noise words.",
               limitations="Documented algorithm: N/(M1+M2-N) >= 0.6. Reltio's noise word list is not "
                           "available - a built-in street-suffix list is used.")

    def compare(self, a: str, b: str) -> CompareResult:
        pa, pb = _PO_BOX.match(a.strip()), _PO_BOX.match(b.strip())
        if pa and pb:
            eq = pa.group(1).lower() == pb.group(1).lower()
            return CompareResult(eq, 1.0 if eq else 0.0, "PO box")
        wa = set(strip_noise(address_words(a), ADDRESS_NOISE))
        wb = set(strip_noise(address_words(b), ADDRESS_NOISE))
        n = len(wa & wb)
        denom = len(wa) + len(wb) - n
        score = n / denom if denom else 0.0
        return CompareResult(score >= 0.6, score, f"common {n}, score {score:.2f}")


@register
class PhoneNumberComparator(Comparator):
    RELTIO_NAME = "PhoneNumberComparator"
    STATUS = DOCUMENTED
    DEFAULT_TOKEN_CLASS = "com.reltio.match.token.PhoneNumberMatchToken"
    DOC = dict(Comparator.DOC,
               parameters="noiseDictionary (invalid numbers) - applied only if available locally",
               input_types="phone-like string",
               case_whitespace="Non-digits removed.",
               normalisation="Right-most 10 digits.",
               limitations="Fewer than 10 digits on either side -> not equal (documented), so 9-digit "
                           "values such as SSNs never match with this comparator.")

    def compare(self, a: str, b: str) -> CompareResult:
        da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
        if len(da) < 10 or len(db) < 10:
            return CompareResult(False, 0.0, "fewer than 10 digits")
        eq = da[-10:] == db[-10:]
        return CompareResult(eq, 1.0 if eq else 0.0)
