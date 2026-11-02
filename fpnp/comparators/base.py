"""Comparator base class. Comparators compare two single, already-cleansed values."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Optional

VERIFIED = "Verified against tenant"
DOCUMENTED = "Documented only"
APPROXIMATION = "Approximation"
UNSUPPORTED = "Unsupported"


@dataclass
class CompareResult:
    equal: bool
    relevance: float
    detail: str = ""


class EngineContext:
    """Behaviour switches and local dictionaries shared by comparators, cleansers and tokens."""

    def __init__(self, case_insensitive: bool = True, treat_blank_as_null: bool = True,
                 noise_dictionaries: Optional[Dict[str, set]] = None,
                 name_dictionary: Optional[Dict[str, str]] = None,
                 regexp_rules: Optional[list] = None,
                 relevance_constraint_mode: str = "veto",
                 relevance_nested_aggregation: str = "flat",
                 double_metaphone_max_length: int = 4):
        self.case_insensitive = case_insensitive
        self.treat_blank_as_null = treat_blank_as_null
        # Ambiguous Reltio relevance semantics, kept switchable (both Approximation):
        #   constraint mode   veto   = unmet strict equals/in/notEquals makes the group not applicable
        #                     ignore = unmet constraint is simply not scored
        #   nested aggregation flat  = each nested sub-attribute is its own weighted operand
        #                      max / mean = sub-attributes of one nested attribute in the same AND form one
        #                      operand (weight = max sub weight, relevance = max / mean of sub relevances)
        self.relevance_constraint_mode = relevance_constraint_mode
        self.relevance_nested_aggregation = relevance_nested_aggregation
        self.noise_dictionaries = noise_dictionaries or {}
        self.name_dictionary = name_dictionary or {}
        self.regexp_rules = regexp_rules or []
        self.double_metaphone_max_length = double_metaphone_max_length
        from .phonetic import configure          # module-level: tokens and blocking share the codes
        configure(double_metaphone_max_length)

    def norm(self, s: str) -> str:
        s = unicodedata.normalize("NFC", s).strip()
        s = re.sub(r"\s+", " ", s)
        return s.lower() if self.case_insensitive else s


class Comparator:
    RELTIO_NAME = ""
    STATUS = APPROXIMATION
    DEFAULT_TOKEN_CLASS = "com.reltio.match.token.ExactMatchToken"
    # documentation surfaced in the Comparator Validation sheet / README
    DOC = {
        "parameters": "",
        "input_types": "string",
        "null_behaviour": "Never called with null: operators handle missing values.",
        "case_whitespace": "",
        "normalisation": "",
        "multi_value": "Operator compares every left/right value pair and keeps the best result.",
        "limitations": "",
    }
    PERMISSIVE = False            # True when equality does not require identical normalised strings

    def __init__(self, params: Optional[dict] = None, ctx: Optional[EngineContext] = None,
                 group: str = "", attribute: str = ""):
        self.params = params or {}
        self.ctx = ctx or EngineContext()
        self.group = group
        self.attribute = attribute

    @property
    def status(self) -> str:
        return self.STATUS

    def compare(self, a: str, b: str) -> CompareResult:  # pragma: no cover - abstract
        raise NotImplementedError
