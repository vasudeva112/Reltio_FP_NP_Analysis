#!/usr/bin/env python3
"""
FPNP Analyzer - Reltio match rule false-positive / false-negative validation.

Usage (Windows CMD, from this folder):
    python fpnp_analyzer.py all        [--settings fpnp.properties]   FP + FN analysis
    python fpnp_analyzer.py fp                                        FP analysis of the Potential Match Report
    python fpnp_analyzer.py fn                                        FN detection over the entity export
    python fpnp_analyzer.py recall                                    candidate recall vs exhaustive (small data)
    python fpnp_analyzer.py validate                                  comparator/operator validation only
    python fpnp_analyzer.py inventory                                 operators/classes/settings in the config
    python fpnp_analyzer.py export     --entity-type Individual --out exports\\Individual.jsonl
    python fpnp_analyzer.py verify     [--sample 50]                  read-only _verifyMatches sample
    python fpnp_analyzer.py synthetic                                 build synthetic data + sample report
    python fpnp_analyzer.py html                                      rebuild FPNP_Report.html from OUTPUT_DIR

Reltio FP / FN analysis engine (attribute scores, overall similarity, client metrics report):
    python fpnp_analyzer.py reltio-fp   [--rule BeneRule01]           FP analysis of the Potential Match Report
    python fpnp_analyzer.py reltio-fn   [--exhaustive]                FN analysis over entities.json exports
    python fpnp_analyzer.py reltio-all                                both + client metrics report
    python fpnp_analyzer.py rules                                     rule / attribute / comparator catalogue
    python fpnp_analyzer.py config-diff --source a.json --target b.json   compare two configurations
    python fpnp_analyzer.py null-probe                                both-null semantics vs Reltio output
    python fpnp_analyzer.py reltio-demo                               validation dataset (14 scenarios) + reports
    streamlit run fpnp_ui.py                                          interactive UI

Any setting can be overridden: --set KEY=VALUE (repeatable), e.g. --set EXHAUSTIVE=yes
"""

import argparse
import csv
import json
import logging
import os
import sys

from fpnp.settings import Settings, TenantConnection, dump_json

log = logging.getLogger("fpnp")


def _overrides(items):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip().upper()] = v.strip()
    return out


def _read_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        if "PythonMatched" in r:
            r["PythonMatched"] = r["PythonMatched"] == "True"
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["all", "fp", "fn", "recall", "validate", "inventory", "export", "verify",
                                     "synthetic", "html", "reltio-fp", "reltio-fn", "reltio-all", "rules",
                                     "config-diff", "null-probe", "reltio-demo"])
    ap.add_argument("--settings", help="analyzer .properties file (default fpnp.properties)")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE")
    ap.add_argument("--entity-type", help="export: entity type name")
    ap.add_argument("--out", help="export: output .jsonl path")
    ap.add_argument("--sample", type=int, help="verify: number of pairs")
    ap.add_argument("--rule", default="", help="reltio-fp: rule for reports without a Rule column ('(infer)' = infer)")
    ap.add_argument("--exhaustive", action="store_true", help="reltio-fn: also compare every pair (small data only)")
    ap.add_argument("--source", help="config-diff: source configuration")
    ap.add_argument("--target", help="config-diff: target configuration")
    args = ap.parse_args(argv)

    s = Settings.load(args.settings, _overrides(args.set))
    from fpnp import pipeline

    if args.mode in ("all", "fp", "fn"):
        files = pipeline.run(s, args.mode)
        print("\n".join(f"{k}: {v}" for k, v in files.items()))
        return 0

    if args.mode.startswith("reltio-") or args.mode in ("rules", "config-diff", "null-probe"):
        return _reltio_mode(args, s)

    if args.mode == "recall":
        files = pipeline.run(s, "fn", recall=True)
        print("\n".join(f"{k}: {v}" for k, v in files.items()))
        return 0

    if args.mode == "validate":
        from fpnp.comparator_validation import run_catalogue
        from fpnp.report_generator import write_csv
        rows = run_catalogue(pipeline.build_context(s))
        path = write_csv(os.path.join(s.output_dir, "detail", "comparator_validation.csv"), rows)
        bad = [r for r in rows if r["Outcome"] != "PASS"]
        print(f"{len(rows)} cases, {len(bad)} failed -> {path}")
        return 1 if bad else 0

    if args.mode == "inventory":
        from fpnp.rule_config_parser import inventory
        groups = pipeline.load_groups(s)
        for k, v in inventory(groups).items():
            print(f"{k}: {dict(v)}")
        for g in groups:
            print(f"- {g.uri} [{g.type}] {g.root.describe()}")
        return 0

    if args.mode == "export":
        from fpnp.entity_export import export_entities
        pipeline.setup_logging(s.output_dir)
        conn = TenantConnection.load(s.tenant_properties)
        et = args.entity_type or conn.entity_type
        out = args.out or os.path.join(s.output_dir, "exports", f"{et}.jsonl")
        n = export_entities(conn, et, out, resume=s.resume)
        print(f"{n} entities -> {out}\nSet ENTITY_EXPORT_PATHS={out} in fpnp.properties")
        return 0

    if args.mode == "verify":
        from fpnp.reltio_verification import choose_sample, run_verification
        pipeline.setup_logging(s.output_dir)
        det = os.path.join(s.output_dir, "detail")
        fp_rows = _read_csv(os.path.join(det, "potential_match_analysis.csv"))
        fn_rows = _read_csv(os.path.join(det, "false_negative_candidates.csv"))
        if not fp_rows and not fn_rows:
            print("Run 'all' (or fp/fn) first - verify samples from its detail files.")
            return 2
        sample = choose_sample(fp_rows, fn_rows, args.sample or s.verify_sample_size, s.verify_seed)
        conn = TenantConnection.load(s.tenant_properties)
        results = run_verification(conn, sample, s.verify_min_interval_sec)
        path = os.path.join(s.output_dir, "verification_results.json")
        old = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        old.update(results)
        dump_json(old, path)
        print(f"{len(results)} pairs verified -> {path}\nRe-run 'all' to merge the results into the workbook.")
        return 0

    if args.mode == "html":
        from fpnp import html_report
        data = html_report.load_outputs(s.output_dir)
        if not data["meta"]:
            print(f"No run found in {s.output_dir} - run 'fp' or 'all' first.")
            return 2
        path = html_report.write(os.path.join(s.output_dir, "FPNP_Report.html"), settings=s,
                                 limitations=pipeline.LIMITATIONS,
                                 workbook=os.path.join(s.output_dir, "FPNP_Analysis.xlsx"), **data)
        print(f"HTML report: {path}")
        return 0

    if args.mode == "synthetic":
        from fpnp import synthetic
        base = os.path.join(s.output_dir, "synthetic")
        paths = synthetic.build(os.path.join(base, "input"))
        s2 = Settings.load(args.settings, {**_overrides(args.set),
                                           "L3_CONFIG_FILE": paths["config"], "REPORT_FILES": paths["report"],
                                           "ENTITY_EXPORT_PATHS": paths["entities"],
                                           "STEWARD_DECISIONS_FILE": paths["steward"],
                                           "ENTITY_TYPES": "Person", "TARGET_RULE": "",
                                           "OUTPUT_DIR": os.path.join(base, "report"), "RESUME": "no"})
        files = pipeline.run(s2, "all", recall=True)
        print("\n".join(f"{k}: {v}" for k, v in {**paths, **files}.items()))
        return 0
    return 1


def _reltio_mode(args, s) -> int:
    from fpnp import reltio_pipeline as rp
    from fpnp.pipeline import setup_logging
    from fpnp.report_generator import write_csv
    setup_logging(s.output_dir)
    if args.mode == "reltio-demo":
        from fpnp import reltio_testdata
        paths = reltio_testdata.build(os.path.join(s.output_dir, "reltio_validation", "input"))
        s.l3_config_file, s.report_files = paths["config"], [paths["report"]]
        s.entity_export_paths, s.steward_decisions_file = [paths["entities"]], paths["steward"]
        s.known_matches_file, s.entity_types, s.target_rule = paths["known"], [], ""
        s.output_dir = os.path.join(s.output_dir, "reltio_validation", "report")
        s.resume = False
        args.mode = "reltio-all"
    try:
        if args.mode == "config-diff":
            from fpnp.reltio_engine.match_rule_parser import compare_configs
            if not (args.source and args.target):
                print("config-diff needs --source and --target")
                return 2
            rows = compare_configs(args.source, args.target, s.entity_types or None)
            path = write_csv(os.path.join(s.output_dir, "config_diff.csv"), rows)
            for r in rows:
                print(f"{r['Change']:8} {r['MatchRule']:30} {r['Aspect']:35} {r['Source'][:40]!s:42} -> {r['Target'][:40]}")
            print(f"{len(rows)} differences -> {path}")
            return 0
        groups = rp.resolve_groups(s)
        if args.mode == "rules":
            from fpnp.reltio_engine.match_rule_parser import attribute_catalog, rule_catalog
            rc, ac = rule_catalog(groups), attribute_catalog(groups)
            p1 = write_csv(os.path.join(s.output_dir, "rule_catalog.csv"), rc)
            p2 = write_csv(os.path.join(s.output_dir, "rule_attribute_catalog.csv"), ac)
            for r in rc:
                print(f"[{r['EntityType']}] {r['MatchRule']} ({r['RuleType']}, {r['PythonFidelity']}): {r['Condition']}")
            print(f"-> {p1}\n-> {p2}")
            return 0
        if args.mode == "null-probe":
            res, files, probe = rp.run_reltio_fp(s, groups, default_rule=args.rule, write=False)
            path = write_csv(os.path.join(s.output_dir, "null_semantics_probe.csv"), probe)
            for r in probe:
                if r["BothNullExactMode"] == "RECOMMENDATION":
                    print(f"{r['MatchRule']}: {r['Recommendation']}")
            print(f"-> {path}")
            return 0
        prog = lambda m, f=0.0: print(f"[{f:4.0%}] {m}", flush=True)
        fp_res = fn_res = None
        files = {}
        if args.mode in ("reltio-fp", "reltio-all"):
            fp_res, f, _ = rp.run_reltio_fp(s, groups, default_rule=args.rule, progress=prog)
            files.update(f)
        if args.mode in ("reltio-fn", "reltio-all"):
            fn_res, f = rp.run_reltio_fn(s, groups, exhaustive=args.exhaustive, progress=prog)
            files.update(f)
        from fpnp.client_metrics_report import write_client_reports
        files.update(write_client_reports(s.output_dir, fp_res, fn_res))
        print("\n".join(f"{k}: {v}" for k, v in files.items()))
        return 0
    except (rp.ConfigMissing, FileNotFoundError) as e:
        print(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
