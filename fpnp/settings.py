"""
Analyzer settings.

Read from a Java-style .properties file (same parser as the existing extractor),
then overridden by environment variables named FPNP_<KEY>. Connection settings are
NOT duplicated here: TENANT_PROPERTIES points at the extractor's properties file
(dev-ov_02.properties) and secrets can be overridden from the environment:

    RELTIO_CLIENT_CREDENTIALS   Base64(client_id:client_secret)
    RELTIO_CLIENT_ID / RELTIO_CLIENT_SECRET
"""

import base64
import dataclasses
import glob
import hashlib
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from .legacy import DEFAULT_AUTH_URL, as_bool, decrypt_if_needed, load_properties

PACKAGE_DIR = Path(__file__).resolve().parent
APP_DIR = PACKAGE_DIR.parent
DEFAULT_SETTINGS_FILE = APP_DIR / "fpnp.properties"


def _split(value: str) -> List[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Settings:
    # inputs
    tenant_properties: str = str(APP_DIR.parent / "dev-ov_02.properties")
    l3_config_file: str = str(APP_DIR.parent / "L3.json")
    entity_types: List[str] = field(default_factory=list)       # blank -> ENTITY_TYPE of tenant props
    target_rule: str = ""                                        # same syntax as the extractor
    report_files: List[str] = field(default_factory=lambda: [str(APP_DIR.parent / "Output.csv")])
    entity_export_paths: List[str] = field(default_factory=list)
    steward_decisions_file: str = ""
    name_dictionary_file: str = ""
    regexp_replace_dictionary_file: str = ""
    noise_dictionary_files: Dict[str, str] = field(default_factory=dict)   # url/name -> local file
    output_dir: str = str(APP_DIR / "output")

    # comparison behaviour (approximations made configurable)
    case_insensitive: bool = True
    treat_blank_as_null: bool = True
    global_candidate_pooling: bool = True
    # Approximations chosen from documentation wording + agreement with this tenant's report (see DESIGN.md 3.2)
    relevance_constraint_mode: str = "ignore"        # veto | ignore
    relevance_nested_aggregation: str = "mean"       # flat | max | mean
    double_metaphone_max_length: int = 4             # code length; 0 = unlimited (see comparators/phonetic.py)
    both_null_exact_mode: str = "fail"               # exact/fuzzy with both values missing: fail | pass | exclude

    # candidate generation
    max_token_collision: int = 300
    max_phrases_per_entity: int = 2000
    blocking_enabled: bool = True
    blocking_max_block_size: int = 1000
    blocking_attribute_pairs: bool = True
    blocking_phonetic: bool = True
    sorted_neighborhood_window: int = 10
    exhaustive: bool = False
    exhaustive_max_entities: int = 5000

    # processing
    batch_size: int = 5000
    workers: int = 1
    resume: bool = True
    excel_row_cap: int = 200000
    write_parquet: bool = True

    # FP risk scoring
    fp_suspect_threshold: float = 5.0
    fp_review_threshold: float = 2.0
    low_margin: float = 0.03
    high_frequency_entities: int = 5
    high_frequency_factor: float = 5.0    # value must also be this many times the attribute's average frequency
    high_frequency_min_distinct_ratio: float = 0.3   # only identifying attributes (distinct values / holders)
    risk_weights: Dict[str, float] = field(default_factory=lambda: {
        "CONSTRAINT_VIOLATED": 3.0,
        "FAILED_OPERAND_STILL_MATCHED": 3.0,
        "POPULATED_VALUES_DIFFER": 2.0,
        "NULL_CONTRIBUTION": 0.5,
        "ONE_SIDED_NULL": 1.5,
        "PERMISSIVE_COMPARATOR": 1.0,
        "CROSS_INSTANCE_NESTED_MATCH": 2.0,
        "MULTI_VALUE_PARTIAL": 0.5,
        "LOW_MARGIN": 1.0,
        "HIGH_FREQUENCY_VALUE": 2.0,
        "PYTHON_DISAGREES": 1.0,
        "APPROXIMATION_DEPENDENT": 0.0,      # informational: almost every relevance evaluation has some
        "RELTIO_RELEVANCE_DIFFERS": 0.0,     # informational: model calibration, not FP evidence
    })

    # comparator score report (FP_Score_Report.xlsx)
    score_operators: List[str] = field(default_factory=lambda: ["fuzzy"])   # operators whose results flag a pair
    score_triggers: List[str] = field(default_factory=lambda: [
        "Disagree", "Partial", "Null", "Phonetic collision", "Fuzzy collision"])
    score_similarity_cutoff: float = 0.85     # Jaro-Winkler below this = collision when the comparator still agreed
    segment_label: str = ""                   # e.g. "Bene Record"
    segment_crosswalk_regex: str = ""         # both entities need a crosswalk matching it, e.g. ^CIF:9000000
    html_include_values: bool = False         # show attribute values (names etc.) in FPNP_Report.html

    # verification (read-only)
    verify_sample_size: int = 50
    verify_min_interval_sec: float = 0.5
    verify_seed: int = 42

    # Reltio FP / FN analysis engine (fpnp.reltio_engine, fp_analysis, fn_analysis)
    scoring_config_file: str = ""             # JSON scoring model; blank = built-in defaults
    known_matches_file: str = ""              # optional CSV of pairs already matched / linked (FN exclusion)
    merge_map_file: str = ""                  # optional CSV loser,winner of merged entities
    fn_blocking_strategies: List[str] = field(default_factory=list)   # blank = every built-in strategy
    fn_custom_blocking_keys: List[str] = field(default_factory=list)  # e.g. LastName+DoB, Identifiers.Value:digits
    fn_max_block_size: int = 500
    fn_min_similarity: float = 60.0           # pairs below this and failing every rule are not reported

    source_file: str = ""

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None, overrides: Optional[Dict[str, str]] = None) -> "Settings":
        s = cls()
        props: Dict[str, str] = {}
        p = Path(path) if path else DEFAULT_SETTINGS_FILE
        if p.exists():
            props = load_properties(str(p))
            s.source_file = str(p.resolve())
        for k, v in os.environ.items():
            if k.startswith("FPNP_"):
                props[k[5:]] = v
        props.update(overrides or {})
        base = p.resolve().parent if p.exists() else APP_DIR
        s._apply(props, base)
        return s

    def _apply(self, props: Dict[str, str], base: Path) -> None:
        def path(v: str) -> str:
            return str((base / v).resolve()) if v and not os.path.isabs(v) else v

        for f in dataclasses.fields(self):
            key = f.name.upper()
            if key not in props or f.name == "source_file":
                continue
            raw = props[key]
            cur = getattr(self, f.name)
            if f.name in ("report_files", "entity_export_paths"):
                setattr(self, f.name, [path(x) for x in _split(raw)])
            elif f.name in ("entity_types", "score_operators", "score_triggers", "fn_blocking_strategies"):
                setattr(self, f.name, _split(raw))
            elif f.name == "fn_custom_blocking_keys":
                setattr(self, f.name, [x.strip() for x in raw.split(";") if x.strip()] if ";" in raw else _split(raw))
                setattr(self, f.name, _split(raw))
            elif f.name in ("risk_weights",):
                upd = dict(cur)
                for item in _split(raw):
                    k, _, val = item.partition(":")
                    upd[k.strip()] = float(val)
                setattr(self, f.name, upd)
            elif f.name == "noise_dictionary_files":
                d = {}
                for item in _split(raw):
                    k, _, val = item.rpartition("=>")
                    d[k.strip()] = path(val.strip())
                setattr(self, f.name, d)
            elif isinstance(cur, bool):
                setattr(self, f.name, as_bool(raw, cur))
            elif isinstance(cur, int):
                setattr(self, f.name, int(raw))
            elif isinstance(cur, float):
                setattr(self, f.name, float(raw))
            elif f.name.endswith(("_file", "_dir", "_properties")):
                setattr(self, f.name, path(raw))
            else:
                setattr(self, f.name, raw)

    def expanded_report_files(self) -> List[str]:
        out: List[str] = []
        for pattern in self.report_files:
            hits = sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern]
            out.extend(h for h in hits if os.path.isfile(h))
        return out

    def public_dict(self) -> dict:
        return dataclasses.asdict(self)


# ----------------------------------------------------------------------
# Tenant connection (read from the extractor's properties + environment)
# ----------------------------------------------------------------------
@dataclass
class TenantConnection:
    auth_url: str
    client_credentials_b64: str
    api_url: str
    entity_type: str
    target_rule: str
    thread_count: int

    @classmethod
    def load(cls, tenant_properties: str) -> "TenantConnection":
        props = load_properties(tenant_properties) if os.path.exists(tenant_properties) else {}
        creds = os.environ.get("RELTIO_CLIENT_CREDENTIALS", "")
        if not creds and os.environ.get("RELTIO_CLIENT_ID"):
            raw = f"{os.environ['RELTIO_CLIENT_ID']}:{os.environ.get('RELTIO_CLIENT_SECRET', '')}"
            creds = base64.b64encode(raw.encode()).decode()
        if not creds:
            creds = decrypt_if_needed(props.get("CLIENT_CREDENTIALS", ""))
        env = os.environ.get("RELTIO_ENVIRONMENT_URL") or props.get("ENVIRONMENT_URL", "")
        tenant = os.environ.get("RELTIO_TENANT_ID") or props.get("TENANT_ID", "")
        if not (creds and env and tenant):
            raise RuntimeError(
                "Reltio connection settings missing: need CLIENT_CREDENTIALS, ENVIRONMENT_URL "
                f"and TENANT_ID in {tenant_properties} or RELTIO_* environment variables")
        return cls(
            auth_url=props.get("AUTH") or props.get("AUTH_URL") or DEFAULT_AUTH_URL,
            client_credentials_b64=creds,
            api_url=f"https://{env}/reltio/api/{tenant}",
            entity_type=props.get("ENTITY_TYPE", "Individual"),
            target_rule=props.get("TARGET_RULE", ""),
            thread_count=int(props.get("THREAD_COUNT") or 5),
        )


def tenant_defaults(tenant_properties: str) -> Dict[str, str]:
    """Non-secret defaults (ENTITY_TYPE, TARGET_RULE) from the extractor's properties."""
    if not os.path.exists(tenant_properties):
        return {}
    props = load_properties(tenant_properties)
    return {k: props[k] for k in ("ENTITY_TYPE", "TARGET_RULE") if k in props}


# ----------------------------------------------------------------------
# Run metadata
# ----------------------------------------------------------------------
def file_sha256(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_fingerprint() -> str:
    h = hashlib.sha256()
    for f in sorted(PACKAGE_DIR.rglob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def run_metadata(settings: Settings, mode: str, extra: Optional[dict] = None) -> dict:
    meta = {
        "mode": mode,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "code_version": __version__,
        "code_fingerprint": code_fingerprint(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "settings_file": settings.source_file,
        "l3_config_file": settings.l3_config_file,
        "l3_config_sha256": file_sha256(settings.l3_config_file),
        "report_files": {f: file_sha256(f) for f in settings.expanded_report_files()},
        "steward_decisions_sha256": file_sha256(settings.steward_decisions_file),
        "parameters": {k: v for k, v in settings.public_dict().items()},
    }
    meta.update(extra or {})
    return meta


def dump_json(obj, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
