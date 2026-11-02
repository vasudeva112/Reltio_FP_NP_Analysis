"""
Orchestration of the Reltio FP / FN analysis engine - shared by the CLI (fpnp_analyzer.py modes
reltio-fp, reltio-fn, rules, config-diff, null-probe) and the Streamlit UI (fpnp_ui.py).

The FP and FN workflows are independent: FP needs a Potential Match Report (+ optional entity JSON),
FN needs entity JSON exports (+ optional Potential Match Report / known-matches file used only to
exclude pairs Reltio already returned). Both need the match-rule configuration - when it is missing a
ConfigMissing error explains how to provide it; no default rules are ever assumed.
"""

import logging
import os
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import steward_decisions
from .candidate_generation import EntityStore
from .exceptions_log import LOG
from .fn_analysis.entity_loader import load_entities, load_known_pairs, load_merge_map
from .fn_analysis.fn_analyzer import FNResult, ReltioFNAnalyzer
from .fn_analysis.fn_report import write_fn_report
from .fp_analysis.fp_analyzer import FPResult, ReltioFPAnalyzer, probe_inputs
from .fp_analysis.fp_report import write_fp_report
from .fp_analysis.potential_match_parser import read_potential_matches
from .report_reader import report_pair_index
from .reltio_engine.match_rule_parser import ConfigError, load_groups
from .reltio_engine.null_semantics import probe
from .reltio_engine.scoring_config import ScoringConfig
from .rule_config_parser import MatchGroup
from .rule_evaluator import RuleEngine
from .settings import Settings, dump_json, run_metadata

log = logging.getLogger("fpnp")


class ConfigMissing(RuntimeError):
    pass


def resolve_groups(s: Settings, config_source=None, entity_types: Optional[Sequence[str]] = None,
                   target_rule: Optional[str] = None) -> List[MatchGroup]:
    src = config_source if config_source is not None else s.l3_config_file
    if src is None or (isinstance(src, str) and not src.strip().startswith(("{", "[")) and not os.path.isfile(src)):
        raise ConfigMissing(
            "Match-rule configuration not available. Provide the tenant configuration export (L3 / "
            "GET /configuration, an entity type with matchGroups, or a list of match groups): set L3_CONFIG_FILE "
            "in fpnp.properties, pass --set L3_CONFIG_FILE=<path>, or upload it in the UI. No default rules are "
            "assumed.")
    types = list(entity_types if entity_types is not None else s.entity_types)
    try:
        groups = load_groups(src, types, s.target_rule if target_rule is None else target_rule)
    except ConfigError as e:
        raise ConfigMissing(str(e)) from e
    if not groups:
        raise ConfigMissing(f"No match rules selected from the configuration (entity types {types or 'all'}, "
                            f"TARGET_RULE '{s.target_rule if target_rule is None else target_rule}').")
    return groups


def engine_for(s: Settings, groups: Sequence[MatchGroup]) -> RuleEngine:
    from .pipeline import build_context
    return RuleEngine(list(groups), build_context(s))


def scoring_for(s: Settings, scoring: Optional[ScoringConfig] = None) -> ScoringConfig:
    if scoring is not None:
        return scoring
    return ScoringConfig.load(s.scoring_config_file) if s.scoring_config_file else ScoringConfig()


# ----------------------------------------------------------------------
def run_reltio_fp(s: Settings, groups: Optional[List[MatchGroup]] = None, report_paths: Optional[List[str]] = None,
                  entity_paths: Optional[List[str]] = None, scoring: Optional[ScoringConfig] = None,
                  default_rule: str = "", run_null_probe: bool = True, out_dir: Optional[str] = None,
                  progress: Optional[Callable[[str, float], None]] = None, write: bool = True
                  ) -> Tuple[FPResult, Dict[str, str], List[dict]]:
    say = progress or (lambda m, f=0.0: None)
    LOG.clear()
    groups = groups or resolve_groups(s)
    engine = engine_for(s, groups)
    cfg = scoring_for(s, scoring)
    reports = report_paths if report_paths is not None else s.expanded_report_files()
    if not reports:
        raise FileNotFoundError("No Potential Match Report given (REPORT_FILES or upload).")
    say("Reading Potential Match Report", 0.05)
    rows = list(read_potential_matches(reports, default_rule))
    entity_paths = entity_paths if entity_paths is not None else s.entity_export_paths
    store = None
    if entity_paths:
        say("Loading entity JSON (OV values of rule attributes)", 0.15)
        store = EntityStore.load(entity_paths, groups, s.treat_blank_as_null)
    steward = steward_decisions.load(s.steward_decisions_file)
    an = ReltioFPAnalyzer(groups, engine, cfg, s, steward, store)
    say(f"Analysing {len(rows)} potential-match rows", 0.3)
    res = an.analyze(rows, progress=lambda n, t: say(f"Analysed {n}/{t} rows", 0.3 + 0.55 * n / max(t, 1)))
    null_rows = []
    if run_null_probe:
        say("Null-semantics calibration", 0.88)
        null_rows = probe(probe_inputs(an, rows), groups, engine.ctx)
    files: Dict[str, str] = {}
    if write:
        out = out_dir or s.output_dir
        say("Writing Excel report", 0.92)
        files = write_fp_report(res, out, s.excel_row_cap, null_rows)
        meta = run_metadata(s, "reltio-fp", {"rows": len(rows), "stats": dict(res.stats),
                                             "scoring": cfg.to_dict(), "entity_paths": entity_paths})
        files["fp_metadata"] = os.path.join(out, "reltio_fp_run.json")
        dump_json(meta, files["fp_metadata"])
    say("Done", 1.0)
    return res, files, null_rows


def run_reltio_fn(s: Settings, groups: Optional[List[MatchGroup]] = None, entity_paths: Optional[List[str]] = None,
                  scoring: Optional[ScoringConfig] = None, report_paths: Optional[List[str]] = None,
                  known_pairs_file: Optional[str] = None, out_dir: Optional[str] = None,
                  exhaustive: bool = False, resume: Optional[bool] = None,
                  progress: Optional[Callable[[str, float], None]] = None, write: bool = True
                  ) -> Tuple[FNResult, Dict[str, str]]:
    say = progress or (lambda m, f=0.0: None)
    LOG.clear()
    groups = groups or resolve_groups(s)
    engine = engine_for(s, groups)
    cfg = scoring_for(s, scoring)
    paths = entity_paths if entity_paths is not None else s.entity_export_paths
    if not paths:
        raise FileNotFoundError("No entities.json export given (ENTITY_EXPORT_PATHS or upload).")
    say("Loading entity exports (streaming, OV values only)", 0.02)
    loaded = load_entities(paths, groups, None, s.treat_blank_as_null, load_merge_map(s.merge_map_file))
    known = load_known_pairs(known_pairs_file if known_pairs_file is not None else s.known_matches_file)
    reports = report_paths if report_paths is not None else s.expanded_report_files()
    if reports:
        for k in report_pair_index(list(read_potential_matches(reports))):
            known.setdefault(k, "returned by Reltio (Potential Match Report)")
    steward = steward_decisions.load(s.steward_decisions_file)
    out = out_dir or s.output_dir
    os.makedirs(out, exist_ok=True)
    an = ReltioFNAnalyzer(groups, engine, loaded, cfg, s, known, steward)
    res = an.run(out, s.resume if resume is None else resume, exhaustive, progress=say)
    files: Dict[str, str] = {}
    if write:
        say("Writing Excel report", 0.97)
        files = write_fn_report(res, out, s.excel_row_cap)
        meta = run_metadata(s, "reltio-fn", {"stats": res.stats, "scoring": cfg.to_dict(), "entity_paths": paths})
        files["fn_metadata"] = os.path.join(out, "reltio_fn_run.json")
        dump_json(meta, files["fn_metadata"])
    return res, files
