# FPNP Analyzer — Reltio match rule false-positive / false-negative validation

Extends the existing Potential Match extractor (`..\Potential_Matches_Extract_v3.py`) into a
match-rule validation engine:

* **Component A – FP analysis.** Re-evaluates every pair in the Potential Match Report with a
  Python model of the configured rule, explains why it matched and scores risk indicators.
* **Component B – rule engine.** Operators, comparator classes (registry) and match-token
  classes parsed from the L3 configuration. Nothing is hardcoded to an entity type or attribute.
* **Component C – FN detection.** Generates candidate pairs over the full entity export
  (Reltio-style token phrases plus recall-oriented blocking), evaluates every match group and
  reports Python matches that Reltio did not return, with reason codes.

Design, inventory of the tenant configuration and every semantic decision: [DESIGN.md](DESIGN.md).

> **Fidelity.** Python *approximates* Reltio. Every operator, comparator and token class is
> labelled *Documented only* or *Approximation* (Sheet 6 and the "Implementation Reference" sheet).
> No result is *Verified against tenant* until you run `verify`. Pairs labelled *Suspected*,
> *Requires Steward Review* or *Python-Detected Candidate* are hypotheses for review. Only
> *Confirmed* labels rest on steward evidence.

---

## 1. Setup

Python 3.11 or later (developed with 3.14). From Windows CMD:

```bat
cd /d D:\Office\Reltio\Python\Potential_Matches\FPNP_Analyzer
py -m pip install -r requirements.txt
py -m pytest -q
```

**metaphone install note.** `metaphone 0.6` fails to build on recent setuptools
(`AttributeError: install_layout`). The package is pure Python; this workaround was verified:
`pip download metaphone --no-deps`, unpack the tarball, and copy its `metaphone` folder into
`site-packages`. Without it, Double Metaphone falls back to single Metaphone, and the fallback is flagged
as an approximation.

**Repository layout.** `fpnp/legacy.py` finds the extractor in `FPNP_EXTRACTOR_DIR`, `extractor/` (this
repository), next to `fpnp_analyzer.py`, or one folder above (the original
`...\Potential_Matches\FPNP_Analyzer` layout).

`py` is the Windows launcher. On this machine `python` resolves to the Microsoft Store alias,
so use `py` (or disable the alias under *Settings › Apps › Advanced app settings › App execution aliases*).

### Configuration – `fpnp.properties`

| Key | Meaning |
|---|---|
| `TENANT_PROPERTIES` | The extractor's properties file. Connection settings and secrets are read from it **at run time only**; nothing is copied. |
| `L3_CONFIG_FILE` | Tenant configuration export containing `matchGroups` |
| `ENTITY_TYPES`, `TARGET_RULE` | Which groups to analyse. `TARGET_RULE` uses the same syntax as the extractor; blank = all groups of the type |
| `REPORT_FILES` | Potential Match Report CSV(s), comma-separated or globs |
| `ENTITY_EXPORT_PATHS` | Entity exports (JSON Lines, JSON array, `{"objects": [...]}` pages, `.gz`, or a directory). Needed for FN detection |
| `STEWARD_DECISIONS_FILE` | Optional ground truth (see §4) |
| `NAME_DICTIONARY_FILE`, `REGEXP_REPLACE_DICTIONARY_FILE`, `NOISE_DICTIONARY_FILES` | Optional local copies of Reltio dictionaries |
| `RELEVANCE_CONSTRAINT_MODE`, `RELEVANCE_NESTED_AGGREGATION` | Ambiguous relevance semantics (DESIGN.md §3.2) |
| `MAX_TOKEN_COLLISION`, `BLOCKING_*`, `SORTED_NEIGHBORHOOD_WINDOW`, `EXHAUSTIVE*` | Candidate generation |
| `BATCH_SIZE`, `WORKERS`, `RESUME`, `EXCEL_ROW_CAP`, `WRITE_PARQUET` | Processing |
| `FP_SUSPECT_THRESHOLD`, `FP_REVIEW_THRESHOLD`, `LOW_MARGIN`, `HIGH_FREQUENCY_*`, `RISK_WEIGHTS` | FP risk scoring |
| `DOUBLE_METAPHONE_MAX_LENGTH` | Double Metaphone code length (default 4, as in the original algorithm and Apache Commons Codec; 0 = unlimited). Affects the comparator, phonetic tokens and blocking |
| `SCORE_OPERATORS`, `SCORE_TRIGGERS`, `SCORE_SIMILARITY_CUTOFF` | Comparator score report: which operators' results can flag a row, which results do, and the Jaro-Winkler cutoff for collisions |
| `SEGMENT_LABEL`, `SEGMENT_CROSSWALK_REGEX` | Optional population segment counted separately (both entities need a matching crosswalk), e.g. `Bene Record` / `^CIF:9000000` |
| `HTML_INCLUDE_VALUES` | `yes` shows compared values (names, addresses) in the HTML report; keep `no` for external sharing |

Write paths with forward slashes (`D:/Office/...`). The properties parser shared with the
extractor treats `\` as an escape character, as Java properties files do.

Any key can be overridden with an environment variable `FPNP_<KEY>`, or on the command line with
`--set KEY=VALUE`.

**Secrets.** Credentials are never stored by the analyzer. They are read from
`TENANT_PROPERTIES` (`CLIENT_CREDENTIALS`, plain Base64 or `ENC(...)`) or, if set, from the
environment: `RELTIO_CLIENT_CREDENTIALS` (Base64 `id:secret`) or `RELTIO_CLIENT_ID` and
`RELTIO_CLIENT_SECRET`, plus optionally `RELTIO_ENVIRONMENT_URL` and `RELTIO_TENANT_ID`.
`run_metadata.json` contains no secrets.

---

## 2. Commands (Windows CMD)

```bat
cd /d D:\Office\Reltio\Python\Potential_Matches\FPNP_Analyzer

:: configuration inventory (operators, comparators, token classes, settings)
py fpnp_analyzer.py inventory

:: Component A: FP analysis of the Potential Match Report (no API calls)
py fpnp_analyzer.py fp

:: read-only export of all entities of a type (uses entities/_scan, resumable)
py fpnp_analyzer.py export --entity-type Individual --out exports/Individual.jsonl
:: then set ENTITY_EXPORT_PATHS=exports/Individual.jsonl in fpnp.properties

:: Component C: FN detection (and FP analysis using full entity values)
py fpnp_analyzer.py fn
py fpnp_analyzer.py all

:: candidate-generation recall vs exhaustive comparison (small datasets only)
py fpnp_analyzer.py recall --set EXHAUSTIVE_MAX_ENTITIES=5000

:: read-only tenant verification of a stratified sample, then merge into the workbook
py fpnp_analyzer.py verify --sample 50
py fpnp_analyzer.py all

:: comparator / operator validation only
py fpnp_analyzer.py validate

:: rebuild the client-facing HTML summary from the last run (no re-analysis, a few seconds)
py fpnp_analyzer.py html

:: synthetic dataset + sample report (self-contained)
py fpnp_analyzer.py synthetic --set OUTPUT_DIR=sample_output

:: examples of overrides
py fpnp_analyzer.py all --set WORKERS=4 --set BATCH_SIZE=20000
py fpnp_analyzer.py fp --set TARGET_RULE=BeneRule01 --set RELEVANCE_CONSTRAINT_MODE=veto
```

### Recommended workflow

1. **Re-run the extractor** (`py ..\Potential_Matches_Extract_v3.py`). It now also writes
   `ReltioRelevance` and `ReltioMatchAction` (last two columns) when `_matches` returns them for
   relevance-based groups. The analyzer then compares Reltio's score with its own on every row
   (`RELTIO_RELEVANCE_DIFFERS`). This calibrates the relevance model far better than anything else.
2. `fp`: review *False Positive Candidates*, sorted by risk score.
3. `export` the entities, then run `all` for FN detection.
4. `verify` a sample and run `all` again. Fix verified implementation errors and record what
   cannot be reproduced locally.
5. Feed steward decisions back through `STEWARD_DECISIONS_FILE` to turn hypotheses into
   confirmed labels.

---

## 3. Outputs (`OUTPUT_DIR`)

`FPNP_Analysis.xlsx`:

| Sheet | Content |
|---|---|
| Executive Summary | Counts with a **Basis** column (confirmed labels / estimate / Reltio report), per group and per entity type, timings, limitations |
| Potential Match Analysis | Every report row: Python evaluation, contributing / failed / differing attributes, null-driven conditions, comparator and token classes, risk, classification, original `src_`/`match_` values, readable trace |
| False Positive Candidates | Confirmed / Suspected FP and Requires Steward Review, sorted by risk |
| False Negative Candidates | Python matches not in the report, with candidate source, reason code, next action and trace |
| Rule-Level Summary | Per group: logic, classes, attributes, candidate / evaluated / satisfied counts, Reltio counts, validation backlog, candidate strategy and risk |
| Comparator Validation | Controlled cases (incl. the null matrix): expected vs Python vs Reltio (when verified), status |
| Processing Exceptions | Invalid JSON, missing attributes, unsupported operators / classes, unparsed settings, approximations, configuration findings |
| Implementation Reference | Per operator / comparator / token class: parameters, null, case and normalisation behaviour, limitations, status |

`FP_Score_Report.xlsx` (written by `fp` and `all`): the comparator-level false-positive view.

| Sheet | Content |
|---|---|
| Summary | Per rule + total + unique pairs: report rows, segment rows and %, every attribute × comparator result (Agree, Close variant, Phonetic / Fuzzy collision, Partial, Disagree, Null), comparator-flagged rows and FP rate, flagged rows by risk classification, relevance average / min / median / max, rows below and just above the lowest threshold, rows per band, Reltio vs Python relevance (when the report has it) |
| False_Positives | One row per pair + rule flagged by a comparator result. Per fuzzy attribute: source and match values, operator, comparator, comparator detail (e.g. Double Metaphone codes, edit distance), comparator relevance, Jaro-Winkler similarity, result. Then relevance score, threshold, margin, band, triggers, reason, score lost per attribute, risk classification, first-row-for-pair flag |
| Attribute_Scores | Long format: every attribute result other than Agree, for all operators |
| Relevance_Weights | Per rule and attribute: operator, comparator, token class, configured and used weight, share of the total, score if only this attribute fails, and whether the pair still matches |
| Rule_Tolerance | Per rule: bands, lowest threshold, headroom, attributes that can fail alone, and attribute pairs that can fail together, while still matching |
| Result_Definitions | Meaning of each comparator result |

Scores come from the analyzer's relevance model, the same one used for the evaluation (`RELEVANCE_*`
settings), through a what-if over the rule tree. Nothing is hardcoded per attribute or rule.

`FPNP_Report.html`: a self-contained, client-presentable summary of the same run (no internet
needed; e-mail it or print it to PDF from the browser). It has headline tiles, key findings in
plain language, pair categories, results by rule, warning signs, the attributes that disagree in
matched pairs, match score distributions with the lowest threshold marked, missed matches (when
an entity export was analysed), a match rules table, the highest-risk pairs from every rule,
next steps and a glossary. Every run writes it; `py fpnp_analyzer.py html` rebuilds it from
`OUTPUT_DIR` without re-running the analysis.

`detail\` holds full CSVs (and Parquet), including `TraceJSON`. A sheet larger than `EXCEL_ROW_CAP`
says "CAPPED EXTRACT" in cell A1 and names the full file. Also written: `run_metadata.json`
(config / report hashes, code version and fingerprint, every parameter), `fpnp_run.log`, and
`checkpoint.sqlite` (resume store).

### Classifications

| FP side | Evidence |
|---|---|
| Confirmed False Positive | steward NOT_A_MATCH |
| Confirmed True Positive | steward MATCH / MERGED |
| Suspected False Positive | risk score ≥ `FP_SUSPECT_THRESHOLD` |
| Requires Steward Review | risk score ≥ `FP_REVIEW_THRESHOLD` |
| Unclassified | otherwise |

Risk indicators: `CONSTRAINT_VIOLATED`, `FAILED_OPERAND_STILL_MATCHED`, `POPULATED_VALUES_DIFFER`,
`NULL_CONTRIBUTION`, `ONE_SIDED_NULL`, `PERMISSIVE_COMPARATOR`, `CROSS_INSTANCE_NESTED_MATCH`,
`MULTI_VALUE_PARTIAL`, `LOW_MARGIN`, `HIGH_FREQUENCY_VALUE`, `PYTHON_DISAGREES`, plus the
informational `APPROXIMATION_DEPENDENT` and `RELTIO_RELEVANCE_DIFFERS` (weight 0).

| FN side | Evidence |
|---|---|
| Reltio Match | pair is in the report (any group) |
| Confirmed False Negative | steward MATCH / MERGED, and Reltio did not return the pair |
| Excluded / Unclassified | steward NOT_A_MATCH |
| Requires Steward Review | `_verifyMatches` confirms Reltio does not match the pair |
| Suspected False Negative | decisive conditions use only documented behaviour |
| Python-Detected Candidate | decisive conditions depend on an approximation |

Reason codes: `TOKENIZATION_MISMATCH`, `CANDIDATE_GENERATION_MISS`, `NULL_HANDLING_MISMATCH`,
`COMPARATOR_BEHAVIOR_MISMATCH`, `CLEANSER_MISMATCH`, `RULE_LOGIC_MISMATCH`, `NEEDS_STEWARD_REVIEW`
(plus `ATTRIBUTE_PATH_MISMATCH`, `NEGATIVE_RULE_APPLIED`, `RULE_NOT_APPLICABLE` and
`INSUFFICIENT_GROUND_TRUTH` in exceptions and counts).

---

## 4. Steward decisions file (optional)

CSV, with headers matched case-insensitively:

```
EntityURI1,EntityURI2,Decision,Reviewer,DecisionDate,Comment
entities/0BB2d7v,entities/00jlmQT,NOT_A_MATCH,jdoe,2026-10-01,different people
entities/0ABC123,entities/0XYZ789,MERGED,jdoe,2026-10-02,
```

`Decision` values: NOT_A_MATCH / MATCH / MERGED and common synonyms. Conflicting decisions for
the same pair are logged and the pair is treated as unlabelled.

---

## 5. Performance and safety

* Entity exports are streamed. Only the attribute paths the selected groups reference are kept
  in memory. Peak Python heap of the FN stage is reported in the Executive Summary; check it on
  a partial export before running the full one.
* Candidate pairs are held as integer index pairs. The number of pairs is driven by
  `BLOCKING_MAX_BLOCK_SIZE` (blocks above it are skipped and logged) and
  `SORTED_NEIGHBORHOOD_WINDOW`. Lower them if memory is tight; raise them for recall.
* `WORKERS>1` evaluates batches in separate processes. Each worker receives a copy of the
  entity store, so memory grows with the worker count.
* Results and evaluated pair keys go to `checkpoint.sqlite` after every batch. An interrupted
  run resumes without re-evaluating any pair. A change to the configuration, data size or
  matching parameters invalidates the checkpoint automatically.
* Exhaustive mode refuses datasets above `EXHAUSTIVE_MAX_ENTITIES`.
* **Read-only.** All tenant calls go through `ReadOnlyClient`, which permits only `GET` and
  `POST` to `entities/_verifyMatches` / `entities/_scan`, throttled with
  `VERIFY_MIN_INTERVAL_SEC` and retried on 429/5xx by the existing client. Merge, unmerge,
  update, delete and configuration changes are refused in code.
* The real FP run on `Output.csv` (52,374 rows) takes about 2.5 minutes of analysis plus about
  2 minutes of Excel writing.

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Python was not found` | Use `py` instead of `python` (Store alias) |
| `No match groups selected` | Check `ENTITY_TYPES` / `TARGET_RULE` against `inventory` output |
| Many `ATTRIBUTE_PATH_MISMATCH` exceptions | The report has no column for a rule attribute (old v1/v2 reports used mapping-file names). Re-run the v3 extractor or provide an entity export |
| `metaphone` missing warning | `py -m pip install metaphone`; until then DoubleMetaphone is approximated |
| FN sheet empty | `ENTITY_EXPORT_PATHS` not set (see Processing Exceptions `INPUT_MISSING`) |
| Excel file locked | Close `FPNP_Analysis.xlsx` before re-running |
| Verify returns `group not in response rules` | API response shape differs on your tenant version; the raw response is stored in `verification_results.json` |

## 7. Changed / added files

Modified (additive; backup kept as `Potential_Matches_Extract_v3.py.bak_before_fpnp`):

* `..\Potential_Matches_Extract_v3.py`: two trailing CSV columns `ReltioRelevance` and
  `ReltioMatchAction` from the `_matches` response, plus a docstring note. Existing columns keep
  their order.

Added under `FPNP_Analyzer\`:

```
DESIGN.md  README.md  requirements.txt  fpnp.properties  fpnp_analyzer.py
fpnp\__init__.py  legacy.py  settings.py  exceptions_log.py  rule_config_parser.py
fpnp\json_loader.py  attribute_extractor.py  cleansers.py  comparator_registry.py
fpnp\comparators\__init__.py  base.py  basic.py  phonetic.py  edit_distance.py  address_phone.py  composite.py
fpnp\operators.py  rule_evaluator.py  match_tokens.py  candidate_generation.py  pair_deduplication.py
fpnp\steward_decisions.py  report_reader.py  explain.py  false_positive_analyzer.py
fpnp\false_negative_analyzer.py  comparator_validation.py  report_generator.py  checkpoint.py
fpnp\reltio_verification.py  entity_export.py  synthetic.py  pipeline.py  html_report.py  fp_scores.py
tests\conftest.py  test_operators_nulls.py  test_comparators.py  test_rule_engine.py
tests\test_candidates.py  test_io_dedup.py  test_integration.py  test_fp_scores.py
sample_output\synthetic\...   (sample report from synthetic data)
output\...                    (FP analysis of the real Output.csv)
```

---

## 8. Reltio FP / FN Analysis Engine (attribute scores, overall similarity, UI)

Added on top of the analyzer above. Everything in sections 1–7 still works unchanged. The new modules
reuse the same rule parser, comparator registry, operators, rule evaluator, streaming loader, OV
extractor, token generator, steward decisions and checkpoint store.

```
fpnp/reltio_engine/   shared comparator & scoring engine
  scoring_config.py     JSON scoring model (bands, weights, null policy, FP / FN thresholds)
  normalization_engine  value as the configured comparator sees it + typed analytical similarity
  comparator_engine     per-attribute result, 0–100 attribute score, explanation
  scoring_engine        weighted overall similarity + SEPARATE rule outcome
  match_rule_parser     load any config shape, rule / attribute catalogue, source-vs-target diff
  null_semantics        measures both-null behaviour against Reltio's own output
  metrics, excel_writer shared metrics, streaming styled Excel writer
fpnp/fp_analysis/     Module A: potential_match_parser, fp_analyzer, fp_report
fpnp/fn_analysis/     Module B: entity_loader, blocking_engine, candidate_generator, fn_analyzer, fn_report
fpnp/reltio_pipeline.py      orchestration shared by CLI and UI
fpnp/client_metrics_report.py client-presentable HTML + Excel metrics
fpnp/reltio_testdata.py      14-scenario validation dataset
fpnp_ui.py                   Streamlit UI
scripts/generate_scale_data.py  large synthetic entities.json for performance tests
config/scoring_config.json   default scoring model (edit, or set SCORING_CONFIG_FILE)
```

### Two numbers, never mixed

| Output | Meaning |
|---|---|
| **Estimated similarity (0–100)** | `Σ(attribute score × weight) / Σ weight` over the rule's *positive* match conditions only. NOT-conditions, negativeRule operands and equals/in filters are reported but never scored. Attributes without values are excluded (`null_policy=exclude`) or scored (`null_policy=score`). Attributes the rule does not use are never read. |
| **Rule evaluation** | Python model of the configured Reltio rule: `MATCH`, `NO MATCH (rule logic …)`, `NO MATCH (relevance below …)`, `NOT APPLICABLE (filter not met)`, `VETOED (negative rule)`, `UNSUPPORTED` |
| **ReltioMatchScore** | Reltio's own relevance, when the Potential Match Report carries `ReltioRelevance`. Kept separate. |

A pair can be 100 % similar and still fail the rule. For example, a NOT condition requires first names to
differ, or an exact identifier is null on both sides.

### Attribute score, per comparator class

| Comparator outcome | Score |
|---|---|
| accepted, identical after the comparator's normalisation | 100 (`EXACT`) |
| accepted different values (fuzzy / phonetic, e.g. Jon ~ John via DoubleMetaphone) | max(strong_min, similarity %) ≤ 99 (`VARIANT_MATCH`) |
| rejected by an exact-class comparator (BasicString, PhoneNumber, RangeNumeric, Custom, CrossMulti) | 0 (`NO_MATCH`). The near-miss similarity is still reported (`exact_class_mismatch=graded` to grade it) |
| rejected by a fuzzy / phonetic comparator | partial band if similarity ≥ floor, else weak band, else 0 |
| no value on one / both sides | `ONE_NULL` / `BOTH_NULL`. The operator decides PASS/FAIL; excluded from the score by default |
| comparator class not implemented | `UNSUPPORTED`: not scored, never claimed equal |

Similarity is typed: dates compare by component (day/month swap, ±1 day), mostly-digit values
(identifiers, phones) by digit edit distance, so `1234567890` vs `9876543210` is *not* "similar". Text uses
the best of Jaro-Winkler, token-sort Jaro-Winkler and word overlap. Conflicts are detected from the data,
not from attribute names: a populated identifier/code mismatch, a date conflict, a configured critical
attribute, or nested values matched across different instances.

### Comparators and operators

| Class | Status |
|---|---|
| BasicStringComparator, DoubleMetaphoneComparator, JaroWinklerComparator, PhoneNumberComparator, CrossMultiComparator | Documented only |
| DynamicDamerauLevenshteinDistance, DamerauLevenshteinDistance, AddressLineComparator, CustomComparator, RangeNumericComparator, SoundexComparator, MetaphoneComparator | Approximation |
| any other class | Unsupported (never equal; reported) |
| exact, fuzzy, exactOrNull, exactOrAllNull, notExact, equals, in, multi, and/or/not | Documented only |
| exactOrNullOrAllNull, notExactSame, notIn, missing, notMissing | Approximation (not found in the Reltio docs available; logged when used) |

**Null semantics.** `exactOrNull` passes for one- or both-sided nulls. `exactOrAllNull` passes only when
both sides are null, and an all-null collection counts as null. exact/fuzzy with both values missing follow
`BOTH_NULL_EXACT_MODE`:

- `fail` is the documented default.
- `pass` lets the operand pass.
- `exclude` makes the operand neutral and leaves it out of the relevance average.

Because you have seen both-null pairs being matched, **measure it**:
`py fpnp_analyzer.py null-probe` re-evaluates every null-affected Potential Match Report row under each
mode. Per rule, it reports how many of Reltio's rows each mode reproduces and the relevance error. The
recommendation is evidence, not proof; confirm a sample with `verify`.

### FP classification (Module A)

`Confirmed False Positive` / `Confirmed Genuine Duplicate` come from steward decisions only. Otherwise:

- **Potential False Positive**: a conflict (populated identifier / date / critical attribute, or nested
  cross-instance match), similarity < `fp.candidate_max_score`, or a match resting on null handling while
  compared values disagree.
- **Likely Genuine Duplicate**: the Python rule reproduces the match, similarity ≥ `fp.genuine_min_score`,
  and there is no conflict.
- **Requires Manual Review**: everything else.

### FN classification (Module B)

Only OV values are used. A Potential Match Report and a known-matches file are optional; they only
exclude pairs Reltio already returned or linked. Pairs that share a crosswalk or appear in `MERGE_MAP_FILE`
are already-merged and are excluded.

| Class | Condition |
|---|---|
| Confirmed False Negative | steward MATCH / MERGED, Reltio did not return the pair |
| High-confidence potential FN | a rule is satisfied with documented semantics only, similarity ≥ `fn.high_min_score`, no null-satisfied condition, no failed attribute, no conflict |
| Medium-confidence potential FN | a rule is satisfied, similarity ≥ `fn.medium_min_score` |
| Requires manual review | a rule is satisfied, but values conflict (in this or another configured rule), nested values matched across instances, unsupported classes, or it rests on nulls while compared values differ |
| Low-confidence candidate | no rule satisfied, similarity ≥ `fn.low_min_score` (near miss, input for rule tuning) |

Reason codes:

- `TOKENIZATION_MISMATCH`: no shared match token, so Reltio never compared the pair.
- `CANDIDATE_GENERATION_MISS`: the only shared token is over-collided.
- `MATCH_EXPECTED_NOT_RETURNED`: tokens are shared and the rule is met. Check rematch, rule changes or comparator differences.
- `RULE_NOT_SATISFIED`: a near miss.
- `STEWARD_CONFIRMED`.

**Blocking.** Candidate pairs are the union of the strategies, so a pair missed by one key is still found
by another:

- `reltio_tokens`: the rule's own match-token phrases. Over-collided phrases are tracked separately.
- `rule_keys`: all exact/fuzzy rule attributes.
- `rule_phonetic`: fuzzy attributes by Double Metaphone code.
- `pairs`: attribute pairs within a rule.
- `custom`: e.g. `LastName+DoB;FirstName:phonetic+LastName;Identifiers.Value:digits;DoB:year;Name:prefix4`.

`single` and `sorted` (neighbourhood) raise recall at a much higher pair count and are opt-in. Blocks above
`FN_MAX_BLOCK_SIZE` (default 100) are skipped and counted. A strategy that would project more pairs than
`FN_MAX_PAIRS_PER_STRATEGY` (default max(1M, 20 × entities)) is skipped as low-selectivity. Both show in
Blocking Analysis. Nullable (`exactOrNull` / `exactOrAllNull`) attributes and filters are never used as
blocking keys. Nothing is compared all-pairs except in `--exhaustive` mode,
which is capped by `EXHAUSTIVE_MAX_ENTITIES`.

### Commands

```bat
:: rule / attribute / comparator catalogue (CSV + console)
py fpnp_analyzer.py rules
:: FP analysis of the Potential Match Report (report values, or entity JSON when ENTITY_EXPORT_PATHS is set)
py fpnp_analyzer.py reltio-fp
:: report without a Rule column: name the rule, or let it be inferred
py fpnp_analyzer.py reltio-fp --rule BeneRule01
:: FN analysis over entities.json exports (resumable; WORKERS>1 = parallel)
py fpnp_analyzer.py reltio-fn --set ENTITY_EXPORT_PATHS=exports/ --set WORKERS=4
:: both + client report
py fpnp_analyzer.py reltio-all
:: both-null calibration against Reltio's output
py fpnp_analyzer.py null-probe
:: compare two configurations (e.g. DEV vs PROD L3 exports)
py fpnp_analyzer.py config-diff --source L3_dev.json --target L3_prod.json
:: 14-scenario validation dataset + all reports
py fpnp_analyzer.py reltio-demo
:: UI
py -m streamlit run fpnp_ui.py
```

If the match-rule configuration is missing, every mode stops with an explanation of how to provide it.
No default rules are assumed. The UI asks for an upload.

### Outputs

| File | Content |
|---|---|
| `Reltio_FP_Analysis_Report.xlsx` | FP Summary (totals, classification, rule-wise / attribute-wise distribution, score distribution, high/medium/low groups, risk indicators, null calibration), FP Pair Details, FP Attribute Details, Scoring Model |
| `Reltio_FN_Analysis_Report.xlsx` | FN Summary (entities, candidate pairs, high/medium/low/review counts, rule-wise counts, reasons, exclusions), FN Pair Details, FN Attribute Details, Blocking Analysis (per key: entities, blocks, pairs, matching candidates, largest blocks), Scoring Model |
| `Reltio_Match_Quality_Report.html` | **client-presentable** FP + FN metrics: headline tiles, findings, verdict and confidence mix, similarity distributions, rule tables, attribute disagreement, missed-match reasons, blocking coverage, method. No attribute values or entity IDs. Prints to PDF. |
| `Reltio_Match_Quality_Metrics.xlsx` | the same metrics as formatted Excel tables with native charts |
| `detail/fp_*.csv`, `detail/fn_*.csv` | full detail (Excel sheets are capped at `EXCEL_ROW_CAP`) |
| `reltio_fp_run.json`, `reltio_fn_run.json`, `fn_checkpoint.sqlite` | run metadata (hashes, parameters, scoring model), FN resume store |

### UI

`py -m streamlit run fpnp_ui.py` (optionally `-- --settings my.properties`).

- **Sidebar**: configuration upload or path, entity types, rules, comparison behaviour (incl. both-null
  mode), scoring model (download as JSON), and *Load validation demo data*.
- **False Positive Analysis**: upload the report and optional entity JSON, run with progress.
- **False Negative Analysis**: upload exports, known-matches and report files; choose blocking strategies,
  custom keys, thresholds, workers, batch size and resume.
- Both result views have filters (match rule, entity type, score range, classification, attribute,
  comparator type), a results grid, and a **pair drill-down** (select a row or pick a pair) showing every
  attribute comparison, normalised values, score, weight, contribution and explanation. Downloads cover
  the Excel report, client HTML and client Excel.
- **Match Rules**: the rule and attribute catalogue, and the source-vs-target comparison.

### Performance (measured)

`scripts/generate_scale_data.py --entities 100000` (84 MB JSON array, 10 % near-duplicates, 5 rules) with
`reltio-fn`, `WORKERS=4`, on a Linux container:

| Stage | Result |
|---|---|
| Streaming load (OV values of 11 rule attributes) | ~9 s |
| Blocking (29 strategies, single process) | 130 s → 32,903 candidate pairs (all-pairs would be ~5 × 10⁹) |
| Evaluation (every pair × 5 rules, 4 workers) + classification | ~110 s |
| Total wall time / peak Python heap | 298 s / 361 MB |

`pairs:FirstName+LastName` was skipped as low-selectivity, because the synthetic data has only 40 × 42
names. Blocking time and memory grow roughly linearly with entities × strategies, and evaluation time
with candidate pairs. For multi-million exports:

- run per entity type;
- keep the default strategies;
- add `WORKERS`;
- rely on `RESUME=yes` (checkpoint per batch).

Blocking runs in one process, and FN workers each get a copy of the pruned entity store.

### Fidelity

Python approximates Reltio. Every comparator and operator is labelled Documented only / Approximation /
Unsupported, and results depending on approximations are graded down. Nothing here claims to reproduce
Reltio exactly until a sample has been checked with the read-only `verify` mode. Synthetic datasets
(`synthetic`, `reltio-demo`, `scripts/generate_scale_data.py`) show behaviour and throughput, not
production FP/FN rates.
