"""
Reuses the existing Potential Match extractor (Potential_Matches_Extract_v3.py)
instead of copying it: properties parsing, Jasypt decrypt, the OAuth client and
the rule-attribute walker all come from there.
"""

import sys
from pathlib import Path

LEGACY_DIR = Path(__file__).resolve().parents[2]       # ...\Potential_Matches
if str(LEGACY_DIR) not in sys.path:
    sys.path.insert(0, str(LEGACY_DIR))

import Potential_Matches_Extract_v3 as v3  # noqa: E402

load_properties = v3.load_properties
as_bool = v3.as_bool
decrypt_if_needed = v3.decrypt_if_needed
ReltioClient = v3.ReltioClient
normalise_rule_uri = v3.normalise_rule_uri
rule_attributes = v3.rule_attributes
format_crosswalks = v3.format_crosswalks
VALUE_SEP = v3.VALUE_SEP
EXCLUDED_CROSSWALK_SOURCES = v3.EXCLUDED_CROSSWALK_SOURCES
DEFAULT_AUTH_URL = v3.DEFAULT_AUTH_URL
DEFAULT_PROPERTIES = v3.DEFAULT_PROPERTIES
