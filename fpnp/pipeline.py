"""Orchestration of the analyzer modes. The CLI (fpnp_analyzer.py) only parses arguments."""

import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from . import __version__
from . import html_report, steward_decisions
from .candidate_generation import CandidateGenerator, EntityStore
from .checkpoint import Checkpoint
from .cleansers import load_name_dictionary, load_noise_dictionary, load_regexp_rules
from .comparator_validation import documentation_rows, run_catalogue
from .comparators.base import EngineContext
from .exceptions_log import LOG
from .false_negative_analyzer import (CONFIRMED_FN, EXCLUDED, PY_CANDIDATE, RELTIO_MATCH, REVIEW as FN_REVIEW,
                                      SUSPECTED_FN, FalseNegativeAnalyzer)
from .fp_scores import RESULT_MEANING, ComparatorScorer
from .false_positive_analyzer import (CONFIRMED_FP, CONFIRMED_TP, REVIEW as FP_REVIEW, SUSPECTED_FP, UNCLASSIFIED,
                                      FalsePositiveAnalyzer)
from .pair_deduplication import pair_key
from .report_generator import Workbook, summary_row, write_csv, write_parquet
from .report_reader import read_reports, report_pair_index
from .rule_config_parser import MatchGroup, inventory, load_config, parse_match_groups, short_attr
from .rule_evaluator import RuleEngine
from .settings import Settings, dump_json, run_metadata, tenant_defaults

log = logging.getLogger("fpnp")

LIMITATIONS = [
    "Python matching approximates Reltio: comparator, operator and token behaviour is labelled per item "
    "(Documented only / Approximation); nothing is 'Verified against tenant' until the verify mode is run.",
    "Candidate generation is not exhaustive outside exhaustive mode: pairs that share no token phrase and no "
    "blocking key are never evaluated.",
    "Reltio's name dictionary, noise dictionaries and regexp alias dictionary are not available locally; "
    "cleansers run as identity unless local copies are configured.",
    "Report-based evaluation cannot distinguish missing / null / empty values and loses nested-instance identity.",
    "Classifications other than 'Confirmed ...' are hypotheses for review, not ground truth.",
]


def setup_logging(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "fpnp_run.log")
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        root.addHandler(logging.StreamHandler(sys.stdout))
    root.setLevel(logging.INFO)
    return path


def build_context(s: Settings) -> EngineContext:
    noise = {}
    for name, path in s.noise_dictionary_files.items():
        if os.path.isfile(path):
            noise[name] = load_noise_dictionary(path)
        else:
            LOG.add("INPUT_ERROR", "Configured noise dictionary file not found", source=path)
    for key, val, ok in (("RELEVANCE_CONSTRAINT_MODE", s.relevance_constraint_mode, ("veto", "ignore")),
                         ("RELEVANCE_NESTED_AGGREGATION", s.relevance_nested_aggregation, ("flat", "max", "mean")),
                         ("BOTH_NULL_EXACT_MODE", s.both_null_exact_mode, ("fail", "pass", "exclude"))):
        if val not in ok:
            raise SystemExit(f"{key} must be one of {ok}, got '{val}'")
    return EngineContext(case_insensitive=s.case_insensitive, treat_blank_as_null=s.treat_blank_as_null,
                         noise_dictionaries=noise, name_dictionary=load_name_dictionary(s.name_dictionary_file),
                         regexp_rules=load_regexp_rules(s.regexp_replace_dictionary_file),
                         relevance_constraint_mode=s.relevance_constraint_mode,
                         relevance_nested_aggregation=s.relevance_nested_aggregation,
                         double_metaphone_max_length=s.double_metaphone_max_length,
                         both_null_exact_mode=s.both_null_exact_mode)


def load_groups(s: Settings) -> List[MatchGroup]:
    if not os.path.isfile(s.l3_config_file):
        raise SystemExit(f"Configuration export not found: {s.l3_config_file}")
    types = s.entity_types or [t for t in [tenant_defaults(s.tenant_properties).get("ENTITY_TYPE")] if t]
    groups = parse_match_groups(load_config(s.l3_config_file), types, s.target_rule)
    if not groups:
        raise SystemExit(f"No match groups selected (entity types {types}, TARGET_RULE '{s.target_rule}')")
    return groups


def candidate_strategy(g: MatchGroup) -> str:
    tok = []
    for n in g.root.walk():
        if n.kind == "op" and n.op in ("exact", "fuzzy", "multi") and n.attribute not in g.ignore_in_token:
            m = g.token_classes.get(n.attribute)
            tok.append(f"{short_attr(n.attribute)}:{m.short if m else 'default'}")
    return ("Token phrase = AND-concatenation of " + (", ".join(tok) if tok else "no tokenised attribute")
            + ". Supplementary blocking on every rule attribute (single, pairs, phonetic, sorted neighbourhood). "
              "Risk: duplicates differing on every token attribute and every blocking key are missed; "
              "not exhaustive.")


# ----------------------------------------------------------------------
def measure_recall(store: EntityStore, groups: List[MatchGroup], engine: RuleEngine, s: Settings) -> List[dict]:
    """Optimised candidate generation vs exhaustive comparison on the loaded (small) dataset."""
    def matched(pairs) -> set:
        out = set()
        recs = store.records
        for (i, j) in pairs:
            for g in groups:
                if g.entity_type == recs[i].entity_type and engine.evaluate(g, recs[i], recs[j], cache=True).matched:
                    out.add((i, j, g.uri))
        return out

    exh = CandidateGenerator(store, groups, engine, s)
    exh_pairs = exh.run(use_tokens=False, use_blocking=False, exhaustive=True)
    if not any(p.exhaustive for p in exh_pairs.values()) and len(store) > 1:
        return [{"MatchGroupURI": "(all)", "Note": "exhaustive mode refused - dataset too large"}]
    tok = CandidateGenerator(store, groups, engine, s)
    tok_pairs = tok.run(use_tokens=True, use_blocking=False, exhaustive=False)
    opt = CandidateGenerator(store, groups, engine, s)
    opt_pairs = opt.run(use_tokens=True, use_blocking=True, exhaustive=False)
    m_exh, m_tok, m_opt = matched(exh_pairs), matched(tok_pairs), matched(opt_pairs)
    rows = []
    for g in groups + [None]:
        sel = (lambda m: {x for x in m if g is None or x[2] == g.uri})
        e, t, o = sel(m_exh), sel(m_tok), sel(m_opt)
        rows.append({
            "MatchGroupURI": g.uri if g else "(all groups)",
            "ExhaustivePairsEvaluated": len(exh_pairs),
            "OptimisedCandidatePairs": len(opt_pairs),
            "MatchesExhaustive": len(e), "MatchesTokenOnly": len(e & t), "MatchesOptimised": len(e & o),
            "RecallTokenOnly": round(len(e & t) / len(e), 4) if e else None,
            "RecallOptimised": round(len(e & o) / len(e), 4) if e else None,
            "Basis": f"estimate: exhaustive comparison on the loaded dataset ({len(store)} entities)",
        })
    return rows


# ----------------------------------------------------------------------
def run(s: Settings, mode: str = "all", recall: bool = False) -> Dict[str, str]:
    t_start = time.time()
    LOG.clear()
    logfile = setup_logging(s.output_dir)
    log.info("FPNP Analyzer %s - mode %s", __version__, mode)
    meta = run_metadata(s, mode)

    groups = load_groups(s)
    inv = inventory(groups)
    log.info("Match groups: %d (%s)", len(groups), dict(inv["types"]))
    ctx = build_context(s)
    engine = RuleEngine(groups, ctx)
    steward = steward_decisions.load(s.steward_decisions_file)

    report_files = s.expanded_report_files()
    report_rows = list(read_reports(report_files)) if report_files else []
    report_idx = report_pair_index(report_rows)
    log.info("Report rows: %d, unique pairs: %d", len(report_rows), len(report_idx))

    store: Optional[EntityStore] = None
    if s.entity_export_paths:
        t0 = time.time()
        store = EntityStore.load(s.entity_export_paths, groups, s.treat_blank_as_null)
        meta["entity_load_seconds"] = round(time.time() - t0, 1)
        log.info("Entities loaded: %s", store.stats)
    elif mode in ("fn", "all"):
        LOG.add("INPUT_MISSING", "No entity export configured (ENTITY_EXPORT_PATHS) - false negative detection "
                                 "skipped. Use 'fpnp_analyzer export' to create one.", severity="ERROR")

    verification = {}
    vfile = os.path.join(s.output_dir, "verification_results.json")
    if os.path.isfile(vfile):
        with open(vfile, encoding="utf-8") as fh:
            verification = json.load(fh)
        log.info("Loaded %d tenant verification results", len(verification))

    fp_rows, fp_pairs, fp_stats = [], [], Counter()
    scorer: Optional[ComparatorScorer] = None
    if mode in ("fp", "all") and report_rows:
        scorer = ComparatorScorer(groups, engine, s)
        fpa = FalsePositiveAnalyzer(groups, engine, s, steward, store, scorer)
        fp_rows, fp_pairs = fpa.analyze(report_rows)
        fp_stats = fpa.stats
        for r in fp_rows:
            v = verification.get(f"{r['PairKey']}##{r.get('MatchGroupURI')}")
            if v:
                r["VerificationResult"] = json.dumps({k: v.get(k) for k in ("reltio_matched", "relevance",
                                                                             "common_tokens", "error")})

    fn_rows, fna = [], None
    if mode in ("fn", "all") and store is not None and len(store):
        fna = FalseNegativeAnalyzer(groups, engine, s, store, report_idx, steward, verification)
        cp = Checkpoint(os.path.join(s.output_dir, "checkpoint.sqlite"), fna.signature(), s.resume)
        try:
            fn_rows = fna.run(cp)
        finally:
            cp.close()

    recall_rows = measure_recall(store, groups, engine, s) if recall and store is not None else []
    validation_rows = run_catalogue(ctx)

    # ---------------- rule-level summary ----------------
    fp_by_group = defaultdict(Counter)
    for r in fp_rows:
        g = r.get("MatchGroupURI", "")
        fp_by_group[g]["reltio_rows"] += 1
        fp_by_group[g]["python_reproduces" if r.get("PythonMatched") else "python_disagrees"] += 1
        fp_by_group[g][r.get("Classification", "")] += 1
    fn_by_group = defaultdict(Counter)
    for r in fn_rows:
        fn_by_group[r["MatchGroupURI"]][r["Classification"]] += 1
    rule_rows = []
    for g in groups:
        sm = g.summary()
        gs = fna.generator.stats.group(g.uri) if fna and fna.generator else {}
        gc = fna.group_counts.get(g.uri, Counter()) if fna else Counter()
        fpc, fnc = fp_by_group[g.uri], fn_by_group[g.uri]
        rec = next((x for x in recall_rows if x.get("MatchGroupURI") == g.uri), {})
        rule_rows.append({
            "MatchGroupURI": g.uri, "Label": g.label, "Type": g.type, "EntityType": g.entity_type,
            "Scope": g.scope, "UseOvOnly": g.use_ov_only, "Logic": sm["Logic"], "Operators": sm["Operators"],
            "ComparatorClasses": sm["ComparatorClasses"], "TokenClasses": sm["TokenClasses"],
            "IgnoreInToken": sm["IgnoreInToken"], "NullValues": sm["NullValues"], "Cleansers": sm["Cleansers"],
            "Weights": sm["Weights"], "ActionThresholds": sm["ActionThresholds"], "Attributes": sm["Attributes"],
            "TokenCandidatePairs": gs.get("token_pairs", ""),
            "OvercollidedPhrases": gs.get("overcollided_phrases", ""),
            "PairsEvaluated": gc.get("evaluated", ""),
            "PairsNotApplicable": gc.get("not_applicable", ""),
            "NegativeRuleHits": gc.get("negative_rule_hit", ""),
            "SatisfyingPythonLogic": gc.get("satisfied", ""),
            "FoundInReltio_ReportRows": fpc["reltio_rows"],
            "ReltioRowsReproducedByPython": fpc["python_reproduces"],
            "ReltioRowsNotReproduced": fpc["python_disagrees"],
            "PythonMatchesInReltio": fnc[RELTIO_MATCH],
            "PythonMatchesNotInReltio": sum(v for k, v in fnc.items() if k != RELTIO_MATCH),
            "RequiringValidation": fpc[SUSPECTED_FP] + fpc[FP_REVIEW] + fnc[SUSPECTED_FN] + fnc[PY_CANDIDATE]
                                   + fnc[FN_REVIEW],
            "RecallOptimised(estimate)": rec.get("RecallOptimised", ""),
            "CandidateStrategyAndRisk": candidate_strategy(g),
            "OtherSettings": sm["OtherSettings"],
        })

    # ---------------- executive summary ----------------
    S = []
    S.append(summary_row("Run", "Code version", f"{__version__} ({meta['code_fingerprint']})"))
    S.append(summary_row("Run", "Configuration file", s.l3_config_file, note=f"sha256 {meta['l3_config_sha256']}"))
    S.append(summary_row("Run", "Report files", "; ".join(report_files) or "(none)"))
    S.append(summary_row("Run", "Mode / started", f"{mode} / {meta['started_at']}"))
    S.append(summary_row("Run", "Match groups analysed", len(groups), note=json.dumps(dict(inv["types"]))))
    S.append(summary_row("Data", "Entities processed",
                         store.stats["entities_loaded"] if store else "n/a - no entity export",
                         "entity export" if store else ""))
    S.append(summary_row("Data", "Potential-match rule-level rows", len(report_rows), "Reltio report"))
    S.append(summary_row("Data", "Potential-match unique pairs", len(report_idx), "Reltio report"))
    fpc_all = Counter(r["Classification"] for r in fp_pairs)
    labelled = fpc_all[CONFIRMED_TP] + fpc_all[CONFIRMED_FP]
    S.append(summary_row("False positives", "Confirmed true positives (pairs)", fpc_all[CONFIRMED_TP], "confirmed labels"))
    S.append(summary_row("False positives", "Confirmed false positives (pairs)", fpc_all[CONFIRMED_FP], "confirmed labels"))
    S.append(summary_row("False positives", "Suspected false positives (pairs)", fpc_all[SUSPECTED_FP],
                         "estimate: risk-score heuristic"))
    S.append(summary_row("False positives", "Requires steward review (pairs)", fpc_all[FP_REVIEW],
                         "estimate: risk-score heuristic"))
    S.append(summary_row("False positives", "Unclassified (pairs)", fpc_all[UNCLASSIFIED], "no evidence either way"))
    S.append(summary_row("False positives", "Precision on labelled pairs",
                         round(fpc_all[CONFIRMED_TP] / labelled, 4) if labelled else "not computed - no confirmed labels",
                         f"confirmed labels ({labelled} pairs)" if labelled else ""))
    rr = sum(1 for r in fp_rows if r.get("PythonMatched"))
    S.append(summary_row("Model agreement", "Report rows reproduced by Python logic",
                         f"{rr} of {len(fp_rows)}" + (f" ({rr / len(fp_rows):.1%})" if fp_rows else ""),
                         "simulated logic vs Reltio output (not ground truth)"))
    fnc_all = Counter(r["Classification"] for r in fn_rows)
    fn_pairs = defaultdict(set)
    for r in fn_rows:
        fn_pairs[r["Classification"]].add(r["PairKey"])
    for cls, basis in ((CONFIRMED_FN, "confirmed labels"), (SUSPECTED_FN, "estimate: documented logic only"),
                       (PY_CANDIDATE, "estimate: depends on approximations"), (FN_REVIEW, "verified sample"),
                       (EXCLUDED, "steward / negative evidence"), (RELTIO_MATCH, "Reltio report")):
        S.append(summary_row("False negatives", f"{cls} (pairs / rule-level rows)",
                             f"{len(fn_pairs[cls])} / {fnc_all[cls]}" if store else "n/a - no entity export", basis))
    if fna:
        for k, v in fna.metrics.get("candidate_sources", {}).items():
            S.append(summary_row("Candidates", f"Candidate pairs: {k}", v, "Python candidate generation"))
        S.append(summary_row("Candidates", "Pairs evaluated", fna.metrics.get("pairs_evaluated"),
                             note="each pair evaluated against every group of its entity type"))
        S.append(summary_row("Candidates", "Pairs satisfying Python rule logic (pairs)",
                             len({r["PairKey"] for r in fn_rows}), "simulated logic"))
        S.append(summary_row("Candidates", "...of which already in the Potential Match Report",
                             len({r["PairKey"] for r in fn_rows if r["Classification"] == RELTIO_MATCH}),
                             "Reltio report"))
        S.append(summary_row("Performance", "FN stage seconds", fna.metrics.get("fn_seconds")))
        S.append(summary_row("Performance", "Peak Python heap (MB)", fna.metrics.get("peak_memory_mb_python_heap")))
    for r in recall_rows:
        if r.get("MatchGroupURI") == "(all groups)":
            S.append(summary_row("Coverage", "Candidate-generation recall (optimised vs exhaustive)",
                                 r.get("RecallOptimised"), r.get("Basis")))
            S.append(summary_row("Coverage", "Candidate-generation recall (token logic only)",
                                 r.get("RecallTokenOnly"), r.get("Basis")))
    for g in groups:
        c, f = fp_by_group[g.uri], fn_by_group[g.uri]
        if c or f:
            S.append(summary_row("By match group", g.name,
                                 f"Reltio rows {c['reltio_rows']}; suspected FP {c[SUSPECTED_FP]}; review {c[FP_REVIEW]}; "
                                 f"confirmed FP {c[CONFIRMED_FP]}; confirmed TP {c[CONFIRMED_TP]}; "
                                 f"suspected FN {f[SUSPECTED_FN]}; candidate FN {f[PY_CANDIDATE]}; "
                                 f"confirmed FN {f[CONFIRMED_FN]}"))
    by_type = defaultdict(Counter)
    for r in fp_pairs:
        by_type[r.get("EntityType", "")][r["Classification"]] += 1
    for r in fn_rows:
        by_type[r["EntityType"]][r["Classification"]] += 1
    for t, c in by_type.items():
        S.append(summary_row("By entity type", t, "; ".join(f"{k}: {v}" for k, v in sorted(c.items()))))
    S.append(summary_row("Performance", "FP stage seconds", fp_stats.get("seconds", "")))
    for lim in LIMITATIONS:
        S.append(summary_row("Limitations", "Coverage / fidelity", lim))

    # ---------------- outputs ----------------
    out = s.output_dir
    det = os.path.join(out, "detail")
    os.makedirs(det, exist_ok=True)
    files = {
        "fp_rule_level": write_csv(os.path.join(det, "potential_match_analysis.csv"), fp_rows),
        "fp_pair_level": write_csv(os.path.join(det, "potential_match_pairs.csv"), fp_pairs),
        "fn": write_csv(os.path.join(det, "false_negative_candidates.csv"), fn_rows),
        "rule_summary": write_csv(os.path.join(det, "rule_level_summary.csv"), rule_rows),
        "validation": write_csv(os.path.join(det, "comparator_validation.csv"), validation_rows),
        "exceptions": write_csv(os.path.join(det, "processing_exceptions.csv"), LOG.rows()),
    }
    if recall_rows:
        files["recall"] = write_csv(os.path.join(det, "candidate_recall.csv"), recall_rows)
    if s.write_parquet:
        for name, rows in (("potential_match_analysis", fp_rows), ("false_negative_candidates", fn_rows)):
            p = write_parquet(os.path.join(det, f"{name}.parquet"), rows)
            if p:
                files[f"{name}_parquet"] = p

    fp_cand = sorted([r for r in fp_rows if r.get("Classification") in (CONFIRMED_FP, SUSPECTED_FP, FP_REVIEW)],
                     key=lambda r: (-(r.get("RiskScore") or 0), r["PairKey"]))
    fn_cand = [r for r in fn_rows if r["Classification"] != RELTIO_MATCH]
    wb = Workbook(os.path.join(out, "FPNP_Analysis.xlsx"), s.excel_row_cap)
    wb.add("Executive Summary", S, cap=False)
    wb.add("Potential Match Analysis", fp_rows, files["fp_rule_level"])
    wb.add("False Positive Candidates", fp_cand, files["fp_rule_level"])
    wb.add("False Negative Candidates", fn_cand, files["fn"],
           note="" if store else "No entity export supplied - FN detection not run")
    wb.add("Rule-Level Summary", rule_rows, files["rule_summary"])
    wb.add("Comparator Validation", validation_rows, files["validation"])
    meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["seconds"] = round(time.time() - t_start, 1)
    meta["fn_metrics"] = fna.metrics if fna else {}
    meta["entity_stats"] = store.stats if store else {}
    meta["fp_stats"] = dict(fp_stats)
    wb.add("Processing Exceptions", LOG.rows(), files["exceptions"])
    wb.add("Implementation Reference", documentation_rows())
    for t in wb.truncations:
        S.append(summary_row("Limitations", "Excel extract capped", t))
    files["workbook"] = wb.save()
    score = write_score_report(scorer, out, det, s.excel_row_cap) if scorer is not None else {}
    files.update(score.get("files", {}))
    files["metadata"] = os.path.join(out, "run_metadata.json")
    files["log"] = logfile
    dump_json(meta, files["metadata"])
    try:
        files["html_report"] = html_report.write(
            os.path.join(out, "FPNP_Report.html"), fp_rows=fp_rows, fp_pairs=fp_pairs, fn_rows=fn_rows,
            rule_rows=rule_rows, recall_rows=recall_rows, exceptions=LOG.rows(), meta=meta, settings=s,
            limitations=LIMITATIONS, workbook=files["workbook"], scores=score.get("data"))
    except Exception:       # the workbook is already written; the summary page must not fail the run
        log.exception("HTML report could not be written")
    log.info("Done in %.1fs. Workbook: %s", meta["seconds"], files["workbook"])
    return files


def write_score_report(scorer: ComparatorScorer, out: str, det: str, row_cap: int) -> dict:
    """FP_Score_Report.xlsx: comparator-level false-positive view (see fp_scores)."""
    data = {"summary": scorer.summary_rows(), "detail": scorer.detail, "attributes": scorer.attribute_rows,
            "weights": scorer.weight_rows(), "tolerance": scorer.tolerance_rows(),
            "results": [{"Result": k, "Meaning": v} for k, v in RESULT_MEANING.items()],
            "segment_label": scorer.segment_label if scorer.segment_re is not None else ""}
    files = {
        "score_summary": write_csv(os.path.join(det, "fp_score_summary.csv"), data["summary"]),
        "score_detail": write_csv(os.path.join(det, "fp_score_detail.csv"), data["detail"]),
        "score_attributes": write_csv(os.path.join(det, "fp_attribute_scores.csv"), data["attributes"]),
        "score_weights": write_csv(os.path.join(det, "relevance_weights.csv"), data["weights"]),
        "score_tolerance": write_csv(os.path.join(det, "rule_tolerance.csv"), data["tolerance"]),
    }
    wb = Workbook(os.path.join(out, "FP_Score_Report.xlsx"), row_cap)
    wb.add("Summary", data["summary"], files["score_summary"], cap=False,
           note="counts per rule; Unique Pairs counts each pair once across rules")
    wb.add("False_Positives", data["detail"], files["score_detail"],
           note="one row per pair + rule flagged by a comparator result")
    wb.add("Attribute_Scores", data["attributes"], files["score_attributes"],
           note="every attribute result other than Agree, all rows")
    wb.add("Relevance_Weights", data["weights"], files["score_weights"], cap=False,
           note="scores computed with the analyzer's relevance model (RELEVANCE_* settings)")
    wb.add("Rule_Tolerance", data["tolerance"], files["score_tolerance"], cap=False)
    wb.add("Result_Definitions", data["results"], cap=False)
    files["score_workbook"] = wb.save()
    log.info("Comparator score report: %d flagged rows -> %s", len(data["detail"]), files["score_workbook"])
    return {"files": files, "data": data}
