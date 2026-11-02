"""
Reltio FP / FN Analysis - interactive UI (Streamlit).

    streamlit run fpnp_ui.py
    streamlit run fpnp_ui.py -- --settings fpnp.properties

Adds an interactive front end to the existing analyzer (the CLI fpnp_analyzer.py and its reports
are unchanged). Tabs:
  False Positive Analysis   Potential Match Report (+ optional entity JSON) -> pair / attribute scores
  False Negative Analysis   entities.json exports -> candidate missed matches, blocking analysis
  Match Rules               rule / attribute / comparator catalogue, source vs target config comparison
Both analyses run through fpnp.reltio_pipeline - the same code as the CLI modes reltio-fp / reltio-fn.
"""

import argparse
import json
import os
import sys
import tempfile

import pandas as pd
import streamlit as st

APP = os.path.dirname(os.path.abspath(__file__))
if APP not in sys.path:
    sys.path.insert(0, APP)

from fpnp import reltio_pipeline as rp  # noqa: E402
from fpnp.client_metrics_report import write_client_reports  # noqa: E402
from fpnp.fn_analysis.blocking_engine import ALL_KINDS, DEFAULT_KINDS  # noqa: E402
from fpnp.fp_analysis.potential_match_parser import INFER_RULE  # noqa: E402
from fpnp.reltio_engine.match_rule_parser import (attribute_catalog, compare_configs, entity_types,  # noqa: E402
                                                  load_rule_config, rule_catalog)
from fpnp.reltio_engine.scoring_config import ScoringConfig  # noqa: E402
from fpnp.settings import Settings  # noqa: E402

st.set_page_config(page_title="Reltio FP / FN Analysis", layout="wide")


# ---------------------------------------------------------------- helpers
def _cli_settings_path():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--settings")
    args, _ = ap.parse_known_args(sys.argv[1:])
    return args.settings


@st.cache_resource
def _upload_dir() -> str:
    return tempfile.mkdtemp(prefix="fpnp_ui_")


def save_uploads(files, sub: str):
    out = []
    d = os.path.join(_upload_dir(), sub)
    os.makedirs(d, exist_ok=True)
    for f in files or []:
        p = os.path.join(d, os.path.basename(f.name))
        with open(p, "wb") as fh:
            fh.write(f.getbuffer())
        out.append(p)
    return out


def split_paths(text: str):
    return [x.strip() for x in (text or "").replace("\n", ",").split(",") if x.strip()]


def progress_cb(bar, label):
    def cb(msg, frac=0.0):
        bar.progress(min(max(float(frac), 0.0), 1.0), text=msg)
        label.caption(msg)
    return cb


def _df(rows) -> pd.DataFrame:
    """DataFrame safe for Arrow: object columns mixing numbers and text become text."""
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows))
    for c in df.columns:
        if df[c].dtype == object:
            kinds = {type(v) for v in df[c].dropna()}
            if len(kinds) > 1:
                df[c] = df[c].map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
    return df


def download(label, path, key, mime="application/octet-stream"):
    if path and os.path.isfile(path):
        with open(path, "rb") as fh:
            st.download_button(label, fh.read(), file_name=os.path.basename(path), mime=mime, key=key)


# ---------------------------------------------------------------- sidebar: configuration
st.sidebar.title("Reltio FP / FN Analysis")
base = Settings.load(_cli_settings_path())
st.sidebar.caption(f"Settings file: {base.source_file or '(defaults)'}")

with st.sidebar.expander("Match-rule configuration", expanded=True):
    cfg_up = st.file_uploader("Upload configuration JSON (tenant L3 / entity type / match groups)",
                              type=["json"], key="cfg_upload")
    cfg_path = st.text_input("...or configuration file path", value=base.l3_config_file if os.path.isfile(
        base.l3_config_file) else "")
    if st.button("Load validation demo data", help="14-scenario validation dataset (synthetic)"):
        from fpnp import reltio_testdata
        st.session_state["demo"] = reltio_testdata.build(os.path.join(_upload_dir(), "demo"))
    demo = st.session_state.get("demo")
    if demo:
        st.caption("Demo data loaded - its configuration, report and entities are pre-filled.")

config_doc, config_err = None, ""
try:
    if cfg_up is not None:
        config_doc = load_rule_config(cfg_up.getvalue())
    elif demo:
        config_doc = load_rule_config(demo["config"])
    elif cfg_path:
        config_doc = load_rule_config(cfg_path)
except Exception as e:                    # shown to the user, never silently replaced by defaults
    config_err = str(e)

if config_doc is None:
    st.sidebar.warning(config_err or "No match-rule configuration loaded. Upload one - no default rules are assumed.")
    types_all = []
else:
    types_all = [r["EntityType"] for r in entity_types(config_doc)]

sel_types = st.sidebar.multiselect("Entity types", types_all,
                                   default=[t for t in base.entity_types if t in types_all] or types_all[:1])
groups = []
if config_doc is not None and sel_types:
    try:
        groups = rp.resolve_groups(base, config_doc, sel_types, "")
    except rp.ConfigMissing as e:
        st.sidebar.error(str(e))
rule_names = [g.name for g in groups]
sel_rules = st.sidebar.multiselect("Match rules (blank = all)", rule_names)
if sel_rules:
    groups = [g for g in groups if g.name in sel_rules]

with st.sidebar.expander("Comparison behaviour"):
    case_ins = st.checkbox("Case-insensitive comparison", base.case_insensitive)
    blank_null = st.checkbox("Treat blank / whitespace as null", base.treat_blank_as_null)
    both_null = st.selectbox("exact / fuzzy when both values are missing", ["fail", "pass", "exclude"],
                             index=["fail", "pass", "exclude"].index(base.both_null_exact_mode),
                             help="Reltio documents 'fail'. Use the null-semantics calibration (FP tab) to see which "
                                  "mode reproduces your tenant's results.")
    rel_mode = st.selectbox("Relevance rules: unmet filter", ["ignore", "veto"],
                            index=["ignore", "veto"].index(base.relevance_constraint_mode))
    nested = st.selectbox("Relevance rules: nested aggregation", ["mean", "max", "flat"],
                          index=["mean", "max", "flat"].index(base.relevance_nested_aggregation))

with st.sidebar.expander("Scoring model"):
    sc_base = ScoringConfig.load(base.scoring_config_file) if base.scoring_config_file else ScoringConfig()
    sc_up = st.file_uploader("Load scoring JSON", type=["json"], key="sc_upload")
    if sc_up is not None:
        sc_base = ScoringConfig.from_dict(json.loads(sc_up.getvalue()))
    c1, c2 = st.columns(2)
    strong = c1.number_input("Strong match min", 0.0, 100.0, sc_base.bands.strong_min)
    partial = c2.number_input("Partial match min", 0.0, 100.0, sc_base.bands.partial_min)
    floor = st.slider("Near-miss similarity floor", 0.0, 1.0, sc_base.partial_similarity_floor)
    exact_mm = st.selectbox("Exact-class comparator mismatch", ["zero", "graded"],
                            index=["zero", "graded"].index(sc_base.exact_class_mismatch))
    null_pol = st.selectbox("Attributes without values", ["exclude", "score"],
                            index=["exclude", "score"].index(sc_base.null_policy))
    wsrc = st.selectbox("Weights", ["rule", "uniform"], index=["rule", "uniform"].index(sc_base.weight_source))
    wjson = st.text_area("Attribute weight overrides (JSON)", json.dumps(sc_base.attribute_weights))
    crit = st.text_input("Critical attributes (comma-separated dotted names)", ", ".join(sc_base.critical_attributes))
    try:
        scoring = ScoringConfig.from_dict({
            **sc_base.to_dict(), "bands": {**sc_base.to_dict()["bands"], "strong_min": strong, "partial_min": partial},
            "partial_similarity_floor": floor, "exact_class_mismatch": exact_mm, "null_policy": null_pol,
            "weight_source": wsrc, "attribute_weights": json.loads(wjson or "{}"),
            "critical_attributes": split_paths(crit)})
    except (ValueError, json.JSONDecodeError) as e:
        st.error(f"Scoring model invalid: {e}")
        scoring = sc_base
    st.download_button("Download scoring JSON", json.dumps(scoring.to_dict(), indent=2), "scoring_config.json")

out_dir = st.sidebar.text_input("Output folder", os.path.join(base.output_dir, "reltio_ui"))


def run_settings() -> Settings:
    s = Settings.load(_cli_settings_path())
    s.case_insensitive, s.treat_blank_as_null, s.both_null_exact_mode = case_ins, blank_null, both_null
    s.relevance_constraint_mode, s.relevance_nested_aggregation = rel_mode, nested
    s.output_dir = out_dir
    if demo:
        s.steward_decisions_file = demo["steward"]
    return s


# ---------------------------------------------------------------- shared result views
def filters(df: pd.DataFrame, attrs: pd.DataFrame, rule_col: str, cls_col: str, score_col: str, key: str):
    c = st.columns(6)
    rules = c[0].multiselect("Match rule", sorted(df[rule_col].dropna().unique()), key=f"{key}_r")
    types = c[1].multiselect("Entity type", sorted(df["EntityType"].dropna().unique()), key=f"{key}_t")
    lo, hi = c[2].slider("Score range", 0, 100, (0, 100), key=f"{key}_s")
    classes = c[3].multiselect("Classification", sorted(df[cls_col].dropna().unique()), key=f"{key}_c")
    att = c[4].multiselect("Attribute", sorted(attrs["Attribute"].dropna().unique()) if len(attrs) else [],
                           key=f"{key}_a")
    cmp_types = c[5].multiselect("Comparator type", sorted(attrs["Comparator"].dropna().unique()) if len(attrs)
                                 else [], key=f"{key}_ct")
    m = pd.Series(True, index=df.index)
    if rules:
        m &= df[rule_col].isin(rules)
    if types:
        m &= df["EntityType"].isin(types)
    sc = pd.to_numeric(df[score_col], errors="coerce")
    if (lo, hi) != (0, 100):
        m &= sc.between(lo, hi)
    if classes:
        m &= df[cls_col].isin(classes)
    if (att or cmp_types) and len(attrs):
        am = pd.Series(True, index=attrs.index)
        if att:
            am &= attrs["Attribute"].isin(att)
        if cmp_types:
            am &= attrs["Comparator"].isin(cmp_types)
        keys = set(zip(attrs.loc[am, "PairID"], attrs.loc[am, "MatchRule"]))
        m &= pd.Series([(p, r) in keys for p, r in zip(df["PairID"], df[rule_col])], index=df.index)
    return df[m]


def drill_down(row: pd.Series, attrs: pd.DataFrame, rule_col: str, cls_col: str, score_col: str, expl_cols):
    st.markdown(f"#### Pair drill-down: `{row['EntityA']}`  vs  `{row['EntityB']}`")
    c = st.columns(4)
    c[0].metric("Estimated similarity", "–" if pd.isna(row.get(score_col)) else f"{float(row[score_col]):.1f}")
    c[1].metric("Rule evaluation", str(row.get("AnalyticalRuleEvaluation", row.get("RuleEvaluation", "")))[:28])
    c[2].metric("Classification", str(row.get(cls_col, ""))[:34])
    rs = row.get("ReltioMatchScore")
    c[3].metric("Reltio match score", "–" if rs in (None, "") or pd.isna(rs) else str(rs))
    st.caption(f"Rule: {row.get(rule_col)}")
    for col in expl_cols:
        if row.get(col):
            st.info(row[col])
    sub = attrs[(attrs["PairID"] == row["PairID"]) & (attrs["MatchRule"] == row[rule_col])]
    show = ["Attribute", "Role", "Operator", "Comparator", "EntityA_Value", "EntityB_Value", "EntityA_Normalized",
            "EntityB_Normalized", "ComparisonResult", "AttributeScore", "Weight", "Contribution",
            "AnalyticalSimilarity", "Conflict", "Explanation"]
    st.dataframe(sub[[c for c in show if c in sub.columns]], hide_index=True, width="stretch")


def results_view(pairs, attrs, rule_col, cls_col, score_col, key, expl_cols):
    df = _df(pairs)
    adf = _df(attrs)
    if df.empty:
        st.warning("No pairs to show.")
        return
    view = filters(df, adf, rule_col, cls_col, score_col, key)
    st.caption(f"{len(view):,} of {len(df):,} rows - select a row for the attribute drill-down")
    cols = [c for c in ["EntityA", "EntityB", "EntityType", rule_col, "ReltioMatchScore", score_col,
                        "AnalyticalRuleEvaluation", "RuleEvaluation", cls_col, "MissedMatchReasonCode",
                        "MatchedAttributes", "UnmatchedAttributes", "Conflicts"] if c in view.columns]
    ev = st.dataframe(view[cols], hide_index=True, width="stretch", on_select="rerun",
                      selection_mode="single-row", key=f"{key}_grid")
    sel = ev.selection.rows if ev and ev.selection else []
    labels = [f"{r.EntityA} vs {r.EntityB} | {r[rule_col]}" for _, r in view.head(2000).iterrows()]
    pick = st.selectbox("...or pick a pair for the drill-down", ["(select)"] + labels, key=f"{key}_pick")
    if sel:
        drill_down(view.iloc[sel[0]], adf, rule_col, cls_col, score_col, expl_cols)
    elif pick != "(select)":
        drill_down(view.iloc[labels.index(pick)], adf, rule_col, cls_col, score_col, expl_cols)


# ---------------------------------------------------------------- tabs
tab_fp, tab_fn, tab_rules = st.tabs(["False Positive Analysis", "False Negative Analysis", "Match Rules"])

with tab_fp:
    st.subheader("False Positive Analysis - Potential Match Report")
    st.caption("Estimated similarity is an analytical score from the rule's attributes and comparators - not "
               "Reltio's match score, which is shown separately when the report has it.")
    c1, c2 = st.columns(2)
    rep_up = c1.file_uploader("Potential Match Report (CSV / XLSX)", type=["csv", "xlsx"], accept_multiple_files=True,
                              key="fp_rep")
    rep_path = c1.text_input("...or report path(s)", ", ".join(base.expanded_report_files()) if not demo else demo["report"])
    ent_up = c2.file_uploader("Entity JSON exports (optional - when the report lacks values)",
                              type=["json", "jsonl", "gz"], accept_multiple_files=True, key="fp_ent")
    ent_path = c2.text_input("...or entity export path(s)", demo["entities"] if demo else ", ".join(base.entity_export_paths))
    default_rule = c1.selectbox("Rule for reports without a Rule column", [INFER_RULE] + rule_names)
    probe_on = c2.checkbox("Null-semantics calibration", True)
    if st.button("Run FP analysis", type="primary", disabled=not groups):
        reports = save_uploads(rep_up, "fp_reports") or split_paths(rep_path)
        ents = save_uploads(ent_up, "fp_entities") or split_paths(ent_path)
        bar, lab = st.progress(0.0), st.empty()
        try:
            res, files, probe = rp.run_reltio_fp(run_settings(), groups, reports, ents, scoring,
                                                 default_rule="" if default_rule == INFER_RULE else default_rule,
                                                 run_null_probe=probe_on, out_dir=out_dir,
                                                 progress=progress_cb(bar, lab))
            files.update(write_client_reports(out_dir, res, st.session_state.get("fn", (None,))[0]))
            st.session_state["fp"] = (res, files, probe)
        except Exception as e:
            st.error(f"FP analysis failed: {e}")
    if "fp" in st.session_state:
        res, files, probe = st.session_state["fp"]
        from fpnp.reltio_engine.metrics import fp_metrics
        m = fp_metrics(res.pairs, res.attributes, res.stats)
        k = st.columns(5)
        k[0].metric("Unique pairs", f"{m['unique_pairs']:,}")
        k[1].metric("Rows analysed", f"{m['evaluated']:,}")
        k[2].metric("Avg similarity", m["score_stats"]["Avg"])
        k[3].metric("Potential FP rows", m["classification_rows"]["Potential False Positive"])
        k[4].metric("Likely genuine rows", m["classification_rows"]["Likely Genuine Duplicate"])
        d = st.columns(4)
        with d[0]:
            download("Excel report", files.get("fp_workbook"), "dl_fp_x")
        with d[1]:
            download("Client report (HTML)", files.get("client_html"), "dl_fp_h", "text/html")
        with d[2]:
            download("Client metrics (Excel)", files.get("client_xlsx"), "dl_fp_m")
        with d[3]:
            download("Attribute details (CSV)", files.get("fp_attributes_csv"), "dl_fp_a", "text/csv")
        results_view(res.pairs, res.attributes, "MatchRule", "FPClassification", "EstimatedSimilarityScore", "fp",
                     ["Explanation", "RuleExplanation"])
        with st.expander("Rule-wise / attribute-wise distribution"):
            st.dataframe(_df(m["rules"]), hide_index=True, width="stretch")
            st.dataframe(_df(m["attributes"]), hide_index=True, width="stretch")
        if probe:
            with st.expander("Null-semantics calibration (both-null exact / fuzzy)"):
                st.dataframe(_df(probe), hide_index=True, width="stretch")

with tab_fn:
    st.subheader("False Negative Analysis - entities.json exports")
    st.caption("Only OV (ov=true) values are compared. Candidates come from blocking keys derived from the rules - "
               "never an all-pairs comparison.")
    c1, c2 = st.columns(2)
    fn_up = c1.file_uploader("entities.json exports", type=["json", "jsonl", "gz"], accept_multiple_files=True,
                             key="fn_ent")
    fn_path = c1.text_input("...or export path(s) / folder",
                            demo["entities"] if demo else ", ".join(base.entity_export_paths))
    kn_up = c2.file_uploader("Known matches / links CSV (optional)", type=["csv"], key="fn_known")
    rp_up = c2.file_uploader("Potential Match Report to exclude returned pairs (optional)", type=["csv", "xlsx"],
                             accept_multiple_files=True, key="fn_rep")
    c3, c4, c5 = st.columns(3)
    kinds = c3.multiselect("Blocking strategies", list(ALL_KINDS), default=list(DEFAULT_KINDS))
    custom = c3.text_area("Custom blocking keys (one per line, e.g. LastName+DoB, FirstName:phonetic+LastName)",
                          "\n".join(base.fn_custom_blocking_keys))
    max_block = c4.number_input("Max block size", 10, 100000, base.fn_max_block_size)
    high = c4.slider("High-confidence min similarity", 0, 100, int(scoring.fn.high_min_score))
    medium = c4.slider("Medium-confidence min similarity", 0, 100, int(scoring.fn.medium_min_score))
    low = c4.slider("Low-confidence min similarity", 0, 100, int(scoring.fn.low_min_score))
    workers = c5.number_input("Worker processes", 1, 64, base.workers)
    batch = c5.number_input("Batch size", 100, 1_000_000, base.batch_size)
    resume = c5.checkbox("Resume from checkpoint", base.resume)
    exhaustive = c5.checkbox("Also exhaustive (small data only)", False)
    if st.button("Run FN analysis", type="primary", disabled=not groups):
        s = run_settings()
        s.fn_blocking_strategies, s.fn_custom_blocking_keys = kinds, split_paths(custom.replace("\n", ","))
        s.fn_max_block_size, s.workers, s.batch_size = int(max_block), int(workers), int(batch)
        s.fn_min_similarity = float(low)
        sc = ScoringConfig.from_dict({**scoring.to_dict(), "fn": {"high_min_score": high,
                                                                  "medium_min_score": medium, "low_min_score": low}})
        paths = save_uploads(fn_up, "fn_entities") or split_paths(fn_path)
        known = (save_uploads([kn_up], "fn_known") or [None])[0] if kn_up else (demo["known"] if demo else None)
        reps = save_uploads(rp_up, "fn_reports") or ([demo["report"]] if demo else [])
        bar, lab = st.progress(0.0), st.empty()
        try:
            res, files = rp.run_reltio_fn(s, groups, paths, sc, reps, known, out_dir, exhaustive, resume,
                                          progress_cb(bar, lab))
            files.update(write_client_reports(out_dir, st.session_state.get("fp", (None,))[0], res))
            st.session_state["fn"] = (res, files)
        except Exception as e:
            st.error(f"FN analysis failed: {e}")
    if "fn" in st.session_state:
        res, files = st.session_state["fn"]
        stt = res.stats
        k = st.columns(6)
        k[0].metric("Entities", f"{stt.get('entities_loaded', 0):,}")
        k[1].metric("Candidate pairs", f"{stt.get('candidate_pairs', 0):,}")
        conf = pd.Series([r["FNConfidence"] for r in res.pairs]).value_counts()
        for i, (lbl, c) in enumerate([("High", "High-confidence potential false negative"),
                                      ("Medium", "Medium-confidence potential false negative"),
                                      ("Review", "Requires manual review"), ("Low", "Low-confidence candidate")], 2):
            k[i].metric(lbl, int(conf.get(c, 0)))
        d = st.columns(4)
        with d[0]:
            download("Excel report", files.get("fn_workbook"), "dl_fn_x")
        with d[1]:
            download("Client report (HTML)", files.get("client_html"), "dl_fn_h", "text/html")
        with d[2]:
            download("Client metrics (Excel)", files.get("client_xlsx"), "dl_fn_m")
        with d[3]:
            download("Pair details (CSV)", files.get("fn_pairs_csv"), "dl_fn_p", "text/csv")
        results_view(res.pairs, res.attributes, "ApplicableMatchRule", "FNConfidence", "OverallSimilarityScore", "fn",
                     ["ReasonForPotentialMissedMatch", "RecommendedAction", "RuleExplanation"])
        with st.expander("Blocking analysis", expanded=False):
            st.dataframe(_df(res.blocking), hide_index=True, width="stretch")
            st.dataframe(_df(res.largest_blocks), hide_index=True, width="stretch")
        st.caption(f"Excluded: {stt.get('excluded')} | seconds: {stt.get('seconds')} | "
                   f"checkpoint: {stt.get('checkpoint')}")

with tab_rules:
    st.subheader("Match rules")
    if not groups:
        st.info("Load a match-rule configuration in the sidebar.")
    else:
        st.dataframe(_df(rule_catalog(groups)), hide_index=True, width="stretch")
        st.markdown("##### Attributes, operators, comparators and match tokens per rule")
        st.dataframe(_df(attribute_catalog(groups)), hide_index=True, width="stretch")
    st.markdown("##### Compare source and target configurations")
    c1, c2 = st.columns(2)
    src_up = c1.file_uploader("Source configuration", type=["json"], key="cmp_src")
    tgt_up = c2.file_uploader("Target configuration", type=["json"], key="cmp_tgt")
    if src_up is not None and tgt_up is not None:
        try:
            diff = compare_configs(src_up.getvalue(), tgt_up.getvalue(), sel_types or None)
            st.caption(f"{len(diff)} differences")
            ddf = _df(diff)
            st.dataframe(ddf, hide_index=True, width="stretch")
            if len(ddf):
                st.download_button("Download differences (CSV)", ddf.to_csv(index=False), "config_diff.csv")
        except Exception as e:
            st.error(f"Comparison failed: {e}")
