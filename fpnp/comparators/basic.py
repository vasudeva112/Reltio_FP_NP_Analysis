from ..comparator_registry import register
from .base import DOCUMENTED, CompareResult, Comparator


@register
class BasicStringComparator(Comparator):
    RELTIO_NAME = "BasicStringComparator"
    STATUS = DOCUMENTED
    DEFAULT_TOKEN_CLASS = "com.reltio.match.token.ExactMatchToken"
    DOC = dict(Comparator.DOC,
               parameters="thresholds (length/percentage tolerance) - not used by this tenant; "
                          "if present it is reported as unsupported",
               case_whitespace="Trimmed, inner whitespace collapsed; case-insensitive by default "
                               "(CASE_INSENSITIVE setting). Reltio docs do not state case handling; "
                               "case-insensitivity comes from third-party documentation.",
               normalisation="Unicode NFC.",
               limitations="Exact string equality after normalisation; relevance 1/0 (documented).")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if self.params.get("thresholds"):
            from ..exceptions_log import LOG
            LOG.add("UNSUPPORTED_PARAMETER", "BasicStringComparator 'thresholds' ignored",
                    group=self.group, attribute=self.attribute)

    def compare(self, a: str, b: str) -> CompareResult:
        eq = self.ctx.norm(a) == self.ctx.norm(b)
        return CompareResult(eq, 1.0 if eq else 0.0)
