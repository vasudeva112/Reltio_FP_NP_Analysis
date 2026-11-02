"""
Reltio comparator & scoring engine shared by the False Positive (fp_analysis) and
False Negative (fn_analysis) modules.

It sits on top of the existing rule engine and does not replace it:

  rule_config_parser / comparators / operators / rule_evaluator   (existing - Reltio semantics)
        |
  reltio_engine.normalization_engine   value as the configured comparator sees it
  reltio_engine.comparator_engine      per-attribute comparison + 0-100 attribute score
  reltio_engine.scoring_engine         weighted overall similarity + separate rule outcome
  reltio_engine.match_rule_parser      config loading (any export shape), rule catalogue, config diff
  reltio_engine.null_semantics         measures how both-null / one-null cases behave on the tenant

Two numbers are kept apart everywhere:
  * Rule evaluation  - the Python model of the configured Reltio rule (PASS / FAIL / ...).
  * Estimated similarity score (0-100) - an analytical score; it is NOT Reltio's match score.
    Reltio's own relevance, when the report has it, is carried in a separate column.
"""

from .scoring_config import ScoringConfig  # noqa: F401
