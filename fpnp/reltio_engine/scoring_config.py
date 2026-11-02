"""
Configurable scoring model (JSON). Every number here is an analytical choice, not a
Reltio threshold. Load with ScoringConfig.load(path) or ScoringConfig.from_dict(d).

Example file (all keys optional):

{
  "bands": {"exact": 100, "strong_min": 80, "partial_min": 40, "weak_min": 1},
  "partial_similarity_floor": 0.70,
  "exact_class_mismatch": "zero",
  "null_policy": "exclude",
  "both_null_score": 100,
  "one_null_score": 0,
  "weight_source": "rule",
  "attribute_weights": {"FirstName": 1.0, "Identifiers.Value": 3.0},
  "critical_attributes": ["Identifiers.Value"],
  "auto_detect_identifiers": true,
  "similarity_groups": {"high": 85, "medium": 60},
  "fp": {"genuine_min_score": 85, "candidate_max_score": 60},
  "fn": {"high_min_score": 85, "medium_min_score": 70, "low_min_score": 60}
}
"""

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass
class Bands:
    exact: float = 100.0          # identical after the comparator's normalisation
    strong_min: float = 80.0      # comparator accepted different values (fuzzy / phonetic / variant)
    partial_min: float = 40.0     # comparator rejected, values still close
    weak_min: float = 1.0         # some similarity

    def label(self, score: Optional[float]) -> str:
        if score is None:
            return "Not scored"
        if score >= self.exact:
            return "Exact match"
        if score >= self.strong_min:
            return "Strong match"
        if score >= self.partial_min:
            return "Partial match"
        if score >= self.weak_min:
            return "Weak similarity"
        return "No match"


@dataclass
class FPThresholds:
    genuine_min_score: float = 85.0      # rule passes, score >= this, no conflict -> Likely genuine duplicate
    candidate_max_score: float = 60.0    # score below this (or a conflict) -> Potential false positive


@dataclass
class FNThresholds:
    high_min_score: float = 85.0
    medium_min_score: float = 70.0
    low_min_score: float = 60.0


@dataclass
class ScoringConfig:
    bands: Bands = field(default_factory=Bands)
    # analytical similarity (0-1) at or above which a rejected comparison is a "partial" match
    partial_similarity_floor: float = 0.70
    # exact-class comparators (BasicString, PhoneNumber, ...) that reject a pair: "zero" scores 0 (the
    # comparator's own verdict); "graded" scores the textual closeness. The near-miss similarity is always
    # reported in its own column.
    exact_class_mismatch: str = "zero"
    # attributes with no value on one / both sides: "exclude" leaves them out of the overall score (reported
    # as null), "score" uses both_null_score / one_null_score
    null_policy: str = "exclude"
    both_null_score: float = 100.0
    one_null_score: float = 0.0
    # "rule": relevance_based rule weights (weights block) where present, else 1; "uniform": all 1
    weight_source: str = "rule"
    attribute_weights: Dict[str, float] = field(default_factory=dict)   # dotted name or URI -> weight
    # attributes whose populated disagreement is a strong "different entity" signal; auto-detection adds
    # exact-compared, mostly-digit values (identifier-like) found in the data - never by attribute name
    critical_attributes: List[str] = field(default_factory=list)
    auto_detect_identifiers: bool = True
    similarity_groups: Dict[str, float] = field(default_factory=lambda: {"high": 85.0, "medium": 60.0})
    fp: FPThresholds = field(default_factory=FPThresholds)
    fn: FNThresholds = field(default_factory=FNThresholds)
    source_file: str = ""

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ScoringConfig":
        d = dict(d or {})
        c = cls()
        if "bands" in d:
            c.bands = Bands(**{**asdict(c.bands), **d.pop("bands")})
        if "fp" in d:
            c.fp = FPThresholds(**{**asdict(c.fp), **d.pop("fp")})
        if "fn" in d:
            c.fn = FNThresholds(**{**asdict(c.fn), **d.pop("fn")})
        for k, v in d.items():
            if hasattr(c, k):
                setattr(c, k, v)
        c.validate()
        return c

    @classmethod
    def load(cls, path: str = "") -> "ScoringConfig":
        if not path:
            return cls()
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Scoring configuration not found: {path}")
        with open(path, encoding="utf-8") as fh:
            c = cls.from_dict(json.load(fh))
        c.source_file = path
        return c

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("source_file", None)
        return d

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        return path

    def validate(self) -> None:
        if self.exact_class_mismatch not in ("zero", "graded"):
            raise ValueError("exact_class_mismatch must be 'zero' or 'graded'")
        if self.null_policy not in ("exclude", "score"):
            raise ValueError("null_policy must be 'exclude' or 'score'")
        if self.weight_source not in ("rule", "uniform"):
            raise ValueError("weight_source must be 'rule' or 'uniform'")
        b = self.bands
        if not (b.exact >= b.strong_min >= b.partial_min >= b.weak_min >= 0):
            raise ValueError("bands must satisfy exact >= strong_min >= partial_min >= weak_min >= 0")
        if not 0 <= self.partial_similarity_floor <= 1:
            raise ValueError("partial_similarity_floor must be between 0 and 1")

    def similarity_group(self, score: Optional[float]) -> str:
        if score is None:
            return "Not scored"
        if score >= self.similarity_groups.get("high", 85):
            return "High"
        if score >= self.similarity_groups.get("medium", 60):
            return "Medium"
        return "Low"

    def weight_override(self, dotted: str, uri: str) -> Optional[float]:
        for k in (uri, dotted):
            if k in self.attribute_weights:
                return float(self.attribute_weights[k])
        return None

    def is_critical(self, dotted: str, uri: str) -> bool:
        return dotted in self.critical_attributes or uri in self.critical_attributes
