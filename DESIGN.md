# FPNP Analyzer – Design

Reltio Match Rule Validation Engine: false-positive (FP) and false-negative (FN)
analysis for the tenant configured in `../dev-ov_02.properties`.

Status labels used throughout this document, the code (`VERIFICATION_STATUS`
attributes) and the workbook:

| Label | Meaning |
|---|---|
| **Verified against tenant** | Python result compared with `_verifyMatches` output for a controlled sample. Nothing is in this state until the verification mode has been run. |
| **Documented only** | Behaviour taken from Reltio documentation; not yet confirmed on this tenant. |
| **Approximation** | Documentation is incomplete or silent; the Python behaviour is a labelled guess. Results that depend on it must not be presented as Reltio-equivalent. |

---

## 1. Existing flow (Phase 1 findings)

```
dev-ov_02.properties ──► Potential_Matches_Extract_v3.run()
   │  load_properties / decrypt_if_needed (Jasypt ENC(...) support)
   │  ReltioClient (OAuth client_credentials, token refresh on 401, HTTP retry)
   ▼
GET  /configuration                → match groups of ENTITY_TYPE
rule_attributes(group)             → every attribute URI referenced by the rule → dotted paths
POST /entities/_scan  filter=equals(type) and range(matches,1,100000) and equals(matchRules,<rule>)
   ▼ (thread pool)
GET  /{entity}/_matches?deep=1     → match list for the rule
     claim_pair(): A→B and B→A collapsed per rule (frozenset)
     extract_values(): OV values only, multi-values joined by " | ", nested paths walked
   ▼
CSV  Rule, RuleLabel, src_/match_ ReltioURI, Crosswalks, <one pair of columns per rule attribute>
```

Observations that matter for the analyzer:

* Columns are **dynamic** (union of every selected rule's attributes). The analyzer
  reads any `src_<path>` / `match_<path>` pair and maps the dotted path back to an
  attribute URI using the rule's entity type – never by column position.
* The report does **not** contain the relevance score or match action label.
* Values are OV-only and de-duplicated, and the report cannot distinguish missing,
  JSON null, empty string or empty array – all become `""`. The analyzer records
  this as a limitation (`VALUE_STATE_UNKNOWN_FROM_REPORT`) for every pair evaluated
  from report values, and prefers entity-export values when exports are supplied.
* Nested sub-attributes are flattened per sub-attribute (`Identifiers.Type`,
  `Identifiers.Value`), so the instance that a value came from is lost.
* Existing outputs: `Output.csv` (52,374 rows; BeneRule01 43,705 / BeneRule02 516 /
  BeneRule03 3,903 / BeneRule04 4,250), `BeneRule02_Output*.csv` (older runs),
  `Output_New.xlsx` (Excel copy).

Reused components (imported, not copied): `load_properties`, `as_bool`,
`decrypt_if_needed`, `ReltioClient`, `normalise_rule_uri`, `rule_attributes`,
`format_crosswalks`, `EXCLUDED_CROSSWALK_SOURCES`, `VALUE_SEP`.

### Inputs located / missing

| Input | Status |
|---|---|
| Potential Match Report | `../Output.csv` (+ older `BeneRule02_Output*.csv`) |
| Tenant configuration (L3) | `../L3.json` – 6 entity types, 39 match groups |
| Connection settings | `../dev-ov_02.properties` (OAuth client credentials, read at run time) |
| **Full entity JSON exports** | **Not found anywhere under `D:\Office\Reltio`.** FN detection is implemented and tested on synthetic data; a read-only `export` mode (reusing `ReltioClient` + `_scan`) is provided to produce the export. |
| **Steward decisions** | **Not found.** An optional CSV input is supported (see README). Without it, no pair can be *Confirmed* FP/TP/FN. |

---

## 2. What the configuration actually uses (inventory of `L3.json`)

### Match group types
`suspect` (10), `automatic` (4), `automatic_grouping` (1), `relevance_based` (24).

### Rule operators found
`and`, `or`, `not`, `exact`, `exactOrNull`, `exactOrAllNull`, `fuzzy`, `multi`,
`equals`, `notEquals`, `in`, plus the helper `nullValues`.
Not present but implemented for completeness/tests: `notIn`, `notExactSame`,
`missing`/`notMissing` (labelled Approximation – semantics not found in docs).

### Comparator classes found
| Class | Uses | Notes |
|---|---|---|
| BasicStringComparator | 161 | |
| DoubleMetaphoneComparator | 43 | |
| JaroWinklerComparator | 9 | `similarityThreshold` 0.95 / 0.98 |
| CustomComparator | 4 | groups of `JaroWinklerComparator` (noiseDictionary `organizationName`) and `RangeNumericComparator` with regex `pattern` (DoB year/month/day ±1) |
| PhoneNumberComparator | 3 | FinancialProfessional BaseRule07 maps it to **SSN** |
| AddressLineComparator | 3 | |
| CrossMultiComparator | 2 | for `multi` group `MultiGroup(FirstName, LastName)` |
| DynamicDamerauLevenshteinDistance | 2 | |
| RangeNumericComparator | nested | only inside CustomComparator groups |

### Match token classes found
ExactMatchToken (135), DoubleMetaphoneMatchToken (40), FuzzyTextMatchToken (8),
CustomMatchToken (5; groups of DoubleMetaphoneMatchToken / DistinctWordsMatchToken
with `threshold 80%`, `useStemmer`, `useSoundex`, remote `noiseDictionary` URLs),
AddressLineMatchToken (4), OrganizationNameMatchToken (2), CrossMultiToken (2),
PhoneNumberMatchToken (1). Group-level `matchTokenClass` (Location rules).

### Cleansers found
`NameDictionaryCleanser` (25 groups; Reltio's built-in name dictionary – not
available locally), `RegexpReplaceCleanser` (RULE10, NameSuffix, remote dictionary
`matchSuffixAlias.txt`, `keepOriginalValue=false`).

### Match-group settings found
`type`, `scope` (INTERNAL / NONE / ALL), `useOvOnly` (all `"true"`),
`scoreStandalone`, `scoreIncremental`, `matchServiceClass` (InternalMatchService),
`rule.ignoreInToken`, `rule.nullValues`, `rule.cleanse`, `rule.weights`,
`rule.actionThresholds`, `rule.matchTokenClass`. No `negativeRule` blocks exist in
this tenant; the parser still supports them. Any key not in the parser's known list
is written to **Processing Exceptions** as `UNPARSED_SETTING`.

### Configuration findings worth investigating (reported, not "fixed")
* FinancialProfessional **BaseRule06**: rule compares `Phone/Number`, comparator is
  mapped to `Phone/FormattedNumber` → `Phone/Number` falls back to the default
  comparator (`ATTRIBUTE_PATH_MISMATCH`).
* FinancialProfessional **BaseRule07**: `SSN` uses `PhoneNumberComparator` (compares
  the last 10 digits; a 9-digit SSN always returns false per documentation).
* Many relevance rules reference attributes in `weights` that are not in the rule and
  vice versa; unweighted attributes default to weight 1.
* BeneRule01/02: `Identifiers/Type` and `Identifiers/Value` are compared as
  independent flattened sub-attributes, so Type can match on one Identifiers
  instance and Value on another (`CROSS_INSTANCE_NESTED_MATCH` risk indicator).

---

## 3. Semantics implemented, with sources

### 3.1 Boolean groups (`suspect`, `automatic`, `automatic_grouping`)
* `and` (object): every operator key inside is an operand; an operator listing N
  attributes contributes N operands. `and` (array): every element is an operand.
  The rule object itself is an implicit `and`. **Documented** (helper operators page).
* `or`: any operand true. `not`: negates its whole block. **Documented.**
* `exact` / `fuzzy`: comparator decides equality; the operators differ only in token
  generation. **Documented.** Both sides must have a value.
* `exactOrNull`: equal, or one side has no value, or both have no value. **Documented.**
* `exactOrAllNull`: equal, or both have no value; one-sided null → false. **Documented.**
* `notExactSame`/`notExact`: inverse of exact, one-sided value → true. **Documented** (notExact).
* `equals`/`in`/`notEquals`/`notIn`: unary constraints evaluated on *each* entity,
  case-insensitive. **Documented.** `notIn` **Approximation** (= notEquals with a list).
* `nullValues`: listed values replaced by null before evaluation (whole value).
  **Documented.** Case-insensitive comparison of the listed value: **Approximation.**
* `multi` + CrossMultiComparator: all attribute permutations, relevance per
  permutation = mean of attribute relevances, best permutation wins. **Documented.**
* Multi-valued attributes: any value pair satisfying the comparator satisfies the
  operand; nested sub-attributes are flattened (cross-instance permutations allowed,
  which is why Reltio documents `notExact` as "keeping nested values grouped").
  **Approximation** (inferred) – flagged in traces.

### 3.2 Relevance-based groups
From Reltio "Configuring relevance-based matching rules":
* Each comparator returns relevance 0–1 (BasicString 0/1; Damerau-Levenshtein,
  Jaro-Winkler graded).
* Weight defaults to 1 for simple attributes; nested/reference attribute = max of its
  sub-attribute weights. Logical sub-operands carry fixed weight 1.
* `AND` = Σ(wᵢ·rᵢ) / Σwᵢ (the documented worked example (1+1+0)/(1+1+0.2)=0.909
  matches this; the formula itself is an image in the docs → **Documented (inferred)**).
* `OR` = max(wᵢ·rᵢ). `NOT` = 1 − r. Multi-valued attribute: max relevance. **Documented.**
* `equals`/`in`/`notEquals` are *strict* constraints by default and prevent tokenisation
  of an entity that fails them; met → weight 0. **Documented.** What an unmet constraint
  does to the *score* is ambiguous ("an unmet one isn't scored"):
  `RELEVANCE_CONSTRAINT_MODE=ignore` (default: not scored, no veto) or `veto` (group not
  applicable). **Approximation.**
* Nested attributes: the documented AND formula has a separate term for nested/reference
  attributes, with weight = max of sub-attribute weights. Python groups the sub-attributes of
  one nested attribute inside the same AND into one operand; its relevance is the mean of the
  sub-attribute relevances (`RELEVANCE_NESTED_AGGREGATION=mean`; `max` and `flat`, which
  scores each sub-attribute separately, are available). **Approximation.**

  Evidence used to choose the defaults: agreement with the 52,374 rows of `Output.csv`
  (report values, BeneRule01-04). This is agreement on *positives only* and favours
  permissive models, so it is not verification:

  | constraint mode | nested | Reltio rows reproduced |
  |---|---|---|
  | veto | flat | 95.83 % |
  | veto | mean | 96.22 % |
  | ignore | flat | 99.39 % |
  | ignore | mean | 99.81 % |
  | ignore | max | 99.87 % |

  The difference between veto and ignore is driven by **1,866 BeneRule01 and 18 BeneRule02 pairs
  Reltio returned although both records' OV `Identifiers.Type` is outside SSN/ITN/EIN**
  (mostly FRPP, DLIC, IDCD). Whatever the mode, such pairs carry the `CONSTRAINT_VIOLATED`
  risk indicator. The extractor now captures Reltio's own relevance
  (`ReltioRelevance`), which allows exact calibration once it is re-run.
* `exactOrNull` / `exactOrAllNull` return relevance 1 for their null cases;
  `exact`/`fuzzy` with a missing side return 0. **Approximation.**
* `actionThresholds`: score within `lo–hi` inclusive; on overlap
  queue_for_review/potential_match beats auto_merge, then higher lower bound wins.
  **Approximation** for boundary inclusivity.

### 3.3 Comparators (detail in `fpnp/comparators/*` docstrings and Sheet 6)
| Class | Python behaviour | Status |
|---|---|---|
| BasicStringComparator | trimmed, case-insensitive string equality, relevance 0/1 | Approximation (case rule from third-party doc; configurable) |
| DoubleMetaphoneComparator | equal if primary/secondary codes intersect (`metaphone` package; jellyfish Metaphone fallback is flagged) | Documented only |
| DynamicDamerauLevenshteinDistance | max edits by shortest length ≤6:1, ≤10:2, ≤20:3, ≤30:4, >30:5; relevance 1−d²/maxlen² | Approximation (doc wording ambiguous on fuzzy operator) |
| JaroWinklerComparator | Jaro-Winkler (p=0.1, prefix 4) ≥ `similarityThreshold` (default 0.9); relevance = similarity | Documented only |
| PhoneNumberComparator | digits only, last 10 must match, <10 digits → false | Documented only |
| AddressLineComparator | PO-box rule, noise-word removal, N/(M1+M2−N) ≥ 0.6 | Approximation (noise list not available) |
| CrossMultiComparator | permutations, mean relevance | Documented only |
| CustomComparator | regex `pattern` extraction per group, AND of groups, relevance = min | Approximation |
| RangeNumericComparator | abs(a−b) ≤ threshold on extracted numbers | Approximation |

### 3.4 Cleansers
* `NameDictionaryCleanser`: Reltio's dictionary is not available; identity transform
  unless a local CSV dictionary (`name_dictionary_file`) is configured. **Approximation.**
* `RegexpReplaceCleanser`: dictionary is a remote URL; applied only if a local copy is
  configured (`regexp_replace_dictionary_file`, lines `pattern<TAB>replacement`).
  Otherwise identity + exception. **Approximation.**
* Remote `noiseDictionary` URLs are never downloaded (no outbound calls besides the
  read-only Reltio API). A local copy can be mapped in settings.

### 3.5 Match tokens / candidate selection
* Token classes generate per-value tokens; tokens of attributes combined with AND
  are concatenated into **token phrases**; profiles sharing a phrase become
  candidates. **Documented.** OR = union of phrases. **Approximation.**
* `exactOrNull`, `exactOrAllNull`, `notExact*`, `not` blocks and constraints produce no
  tokens; `ignoreInToken` suppresses tokens; entities failing a strict top-level
  constraint get no tokens for that group. **Documented** (constraints, ignoreInToken)
  / **Approximation** (`not`).
* Phrases shared by more than 300 profiles are over-collided and skipped by Reltio
  (`checkOvercollisionedTokens=true`). **Documented.** Pairs reachable only through
  such phrases are reported as `CANDIDATE_GENERATION_MISS`.
* Token class used: mapped class → group `matchTokenClass` → comparator's documented
  default token class → ExactMatchToken (recorded as `DEFAULT_TOKEN_CLASS`).
* Global candidate pooling: a pair found by any group's phrases is evaluated by every
  group of the entity type (`global_candidate_pooling=true`). The report shows pairs
  that no group's phrase would produce under strict per-group logic (e.g. BeneRule01
  pairs with phonetically different first names), which is consistent with pooling.
  **Approximation** – resolved per pair by verification (`candidatesDiscovery`).

---

## 4. Modules and data flow

```
FPNP_Analyzer/
  fpnp_analyzer.py            CLI (modes: fp, fn, all, validate, verify, export, synthetic, recall)
  fpnp/
    settings.py               properties + env overrides; run metadata (hashes, version)
    legacy.py                 imports reusable parts of Potential_Matches_Extract_v3
    exceptions_log.py         Processing Exceptions collector
    rule_config_parser.py     L3 → MatchGroup (rule tree, mappings, weights, thresholds, settings)
    json_loader.py            streaming JSON array / JSONL / directory loader
    attribute_extractor.py    URI path → AttrValues (state: PRESENT/MISSING/NULL/EMPTY_STRING/WHITESPACE/EMPTY_LIST)
    cleansers.py              cleanse adapters + nullValues
    comparator_registry.py    registry (decorator) – new classes need no engine change
    comparators/              basic, phonetic, edit distance, address/phone, composite
    operators.py              operator evaluators (boolean + relevance), traces
    rule_evaluator.py         evaluates a MatchGroup for a pair; negative rules; thresholds
    match_tokens.py           token classes + phrase builder
    candidate_generation.py   token index (+overcollision), recall blocking, sorted neighbourhood, exhaustive
    pair_deduplication.py     deterministic pair keys, pair/rule aggregation
    steward_decisions.py      optional ground truth
    report_reader.py          reads v3 Potential Match Report CSVs (dynamic columns)
    false_positive_analyzer.py
    false_negative_analyzer.py
    comparator_validation.py  catalogue of controlled cases (Sheet 6, also used by tests)
    report_generator.py       Excel (7 sheets) + CSV/Parquet detail
    checkpoint.py             SQLite store: resume, never evaluate a pair twice
    reltio_verification.py    read-only _verifyMatches / _matches sampler
    entity_export.py          read-only _scan export to JSONL
    synthetic.py              synthetic dataset + report + steward decisions
  tests/
```

Data flow:

```
L3.json ──► rule_config_parser ──► MatchGroups ─┬─────────────────────────────┐
                                                 │                             │
Output.csv ─► report_reader ─► pairs ─► FP analyzer ◄── steward decisions      │
                                         │  (rule_evaluator per pair)           │
entity export ─► json_loader ─► attribute_extractor ─► EntityStore             │
                       │                                                       ▼
                       └──► candidate_generation (tokens | blocking | exhaustive)
                                   ──► rule_evaluator ──► FN analyzer ◄── report pairs
                                                              │
                          reltio_verification (sample) ───────┤
                                                              ▼
                                   report_generator: workbook + CSV/Parquet + run_metadata.json
```

Comparator evaluation (`comparators/`) is separate from rule evaluation
(`operators.py`, `rule_evaluator.py`); operators never compare strings themselves.

---

## 5. Classification

### FP (pairs present in the report)
1. Steward NOT_A_MATCH → **Confirmed False Positive**.
2. Steward MATCH / MERGED → **Confirmed True Positive**.
3. Otherwise risk indicators are scored (weights configurable):
   `FAILED_OPERAND_STILL_MATCHED`, `NULL_CONTRIBUTION`, `ONE_SIDED_NULL`,
   `PERMISSIVE_COMPARATOR`, `CROSS_INSTANCE_NESTED_MATCH`, `MULTI_VALUE_PARTIAL`,
   `LOW_MARGIN`, `HIGH_FREQUENCY_VALUE`, `POPULATED_VALUES_DIFFER`,
   `PYTHON_DISAGREES`, `APPROXIMATION_DEPENDENT`.
   score ≥ `fp_suspect_threshold` → **Suspected False Positive**;
   ≥ `fp_review_threshold` → **Requires Steward Review**; else **Unclassified**.
   A Reltio match is never labelled FP without evidence.

### FN (pairs satisfying Python logic)
1. Same entity (already merged) → skipped.
2. Pair in the report (any group) → **Reltio Match**.
3. Steward NOT_A_MATCH → **Excluded / Unclassified**.
4. Steward MATCH/MERGED → **Confirmed False Negative**.
5. Verified by `_verifyMatches` as not matched → **Requires Steward Review**.
6. Decisive operands use only Documented/Verified components → **Suspected False Negative**;
   otherwise → **Python-Detected Candidate**.

Reason codes follow the spec: `TOKENIZATION_MISMATCH` (no shared phrase; found by
blocking), `CANDIDATE_GENERATION_MISS` (shared phrase over-collided),
`NULL_HANDLING_MISMATCH`, `COMPARATOR_BEHAVIOR_MISMATCH`, `CLEANSER_MISMATCH`,
`ATTRIBUTE_PATH_MISMATCH`, `RULE_LOGIC_MISMATCH`, `NEGATIVE_RULE_APPLIED`,
`RULE_NOT_APPLICABLE`, `INSUFFICIENT_GROUND_TRUTH`, `NEEDS_STEWARD_REVIEW`.

---

## 6. Scale and completeness limitations

* Candidate generation is **not exhaustive** in production mode. Recall blocking uses,
  per group: every single attribute (blocks larger than `blocking_max_block_size`
  skipped and reported), every pair of attributes (exact and phonetic keys), and a
  sorted neighbourhood per attribute. Pairs that differ on *every* blocking key and
  share no token phrase are never seen. The `recall` mode measures this against
  exhaustive comparison on a small dataset; the result is an estimate for that
  dataset only.
* Entity values needed by the selected groups are held in memory (pruned to the
  referenced attribute paths). Pair keys are stored in SQLite for resume.
* Remote dictionaries (name dictionary, noise lists, regexp alias list) are not
  available → cleanser/noise behaviour is approximated and every affected
  evaluation carries `APPROXIMATION_DEPENDENT`.
* Report-based FP evaluation cannot distinguish null/missing/empty values.
* No precision/recall figure is ground truth unless its basis says "confirmed labels".

## 7. Unknown / unsupported behaviour (also listed in Processing Exceptions)
* Exact token algorithms of DistinctWordsMatchToken (`threshold 80%`, `useStemmer`,
  `useSoundex`), FuzzyTextMatchToken, OrganizationNameMatchToken, AddressLineMatchToken.
* Contents of Reltio's built-in name dictionary and noise dictionaries.
* Whether Reltio pools candidates across match groups for this tenant.
* Relevance of `exact`/`fuzzy` when one side is null in relevance-based groups.
None of these blocks implementation: each is implemented as a labelled approximation
and is a target for the `verify` mode.

---

## 8. Reltio FP / FN Analysis Engine (extension)

Requirements: Reltio-specific attribute-wise and overall scoring for FP analysis of the Potential Match
Report, and independent FN analysis over `entities.json` exports, both on one comparator/scoring engine.
This extends the components above and replaces none of them. User guide: README §8.

### 8.1 Reuse

| Need | Reused component | Added |
|---|---|---|
| Rule / comparator / token parsing | `rule_config_parser` | `reltio_engine.match_rule_parser`: any export shape, catalogues, config diff |
| Reltio comparison verdict | `operators`, `comparators/*`, `rule_evaluator` | `exactOrNullOrAllNull` (labelled), `BOTH_NULL_EXACT_MODE`, Soundex/Metaphone |
| Attribute score / explanation | trace `LeafResult`s from `RuleEngine.evaluate` | `comparator_engine`, `normalization_engine` |
| Overall score + rule outcome | `GroupEvaluation` | `scoring_engine` (`PairScorer`, `RuleOutcome`) |
| Report reading | `report_reader` (+ extractor columns) | alias URI columns, XLSX, reports without a Rule column |
| FP risk signals | `explain.risk_indicators`, `false_positive_analyzer._frequency` | verdicts, explanations |
| Entity loading | `json_loader` (streaming), `attribute_extractor` (OV, null states), `EntityStore` | crosswalk / metadata capture, merge detection |
| Candidate generation | `match_tokens.GroupTokenizer`, `phonetic.double_metaphone` | `blocking_engine` with per-strategy statistics |
| Resume / parallel | `checkpoint.Checkpoint`, process-pool pattern | FN payloads, strategy counters |

### 8.2 Decisions

* **Similarity ≠ Reltio score ≠ rule outcome.** Three separate columns. The analytical score never
  overrides the rule evaluation, and Reltio's relevance is never recomputed.
* **The comparator verdict comes from the configured class.** Generic string similarity is only used to
  *grade* closeness. It never decides equality, and exact-class mismatches score 0 by default.
* **Scoring scope.** Only positive match conditions of the applicable rule are scored. NOT operands,
  negativeRule operands and filters are listed with their role. The weights are the rule's relevance
  weights (relevance_based) or 1, and can be overridden per attribute.
* **Nulls.** Excluded from the similarity by default, so missing data does not inflate it. The operator
  semantics for the rule outcome are unchanged and measurable (`null_semantics.probe`).
* **Conflicts are data-driven.** Identifier-like means mostly digits and exact-compared, dates are parsed,
  and critical attributes are configurable. No attribute names are hardcoded.
* **Best rule per FN pair.** A satisfied rule first, then higher similarity, then documented-only fidelity,
  then fewer null-satisfied conditions.
* **FN confidence never exceeds the evidence.** Approximations, null reliance, failed attributes, or a
  conflict seen by any configured rule cap or downgrade the class. "Confirmed" requires a steward decision.
* **Scale.** Streaming load keeps only rule attributes. Pairs are stored as 64-bit integer keys with
  strategy bitmasks. Blocks are capped. Excel detail sheets stream in openpyxl write-only mode with a row
  cap, and full detail goes to CSV. Checkpoint/resume runs per batch.

### 8.3 Known limitations

* Same as §6/§7. Also, the analytical similarity is a reviewing aid. Its bands are configurable choices,
  not Reltio thresholds.
* The default blocking is selective. Pairs that differ on every blocking key are not seen (enable
  `single` / `sorted` / custom keys, or measure with `--exhaustive` on a sample).
* FN worker processes each receive a copy of the pruned entity store (memory grows with `WORKERS`).
* `MATCH_EXPECTED_NOT_RETURNED` cannot distinguish "not rematched yet" from a tenant behaviour
  difference without `_verifyMatches` (read-only `verify` mode).
