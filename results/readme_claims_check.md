# README claims → source check

Every claim-bearing sentence in `README.md`, with its source. Evaluation numbers are from
`results/readme_numbers.md` (which itself cites the underlying files); code facts cite the
code; design facts cite `eval/PREREGISTRATION.md`.

## Intro

| claim | source |
|---|---|
| read-only text-to-SQL MCP server for PostgreSQL, runs locally over stdio | `src/queryguard/server.py` (module docstring, "Transport is stdio"); `CLAUDE.md` |
| ships with a preregistered evaluation | `eval/PREREGISTRATION.md` |

## Result in brief / Results (accuracy + exploratory)

| claim | source |
|---|---|
| baseline 81/150 = 54.0% | `results/readme_numbers.md` → Primary accuracy (baseline correct count) |
| main 75/150 = 50.0% | `results/readme_numbers.md` → Primary accuracy (main correct count) |
| paired difference −4.0 points | `results/readme_numbers.md` → Primary accuracy (point estimate) |
| 95% CI [−11.3, +3.3] | `results/readme_numbers.md` → Inference (paired bootstrap CI) |
| non-inferiority margin 10 points | `results/readme_numbers.md` → Inference (non-inferiority margin) |
| McNemar p = 0.3616 | `results/readme_numbers.md` → Inference (McNemar p) |
| non-inferiority not demonstrated; inconclusive | `results/readme_numbers.md` → Inference (preregistered verdict) |
| Wilson 95% baseline 46.0–61.8% | `results/readme_numbers.md` → Inference (Wilson baseline) |
| Wilson 95% main 42.1–57.9% | `results/readme_numbers.md` → Inference (Wilson main) |
| 2×2 = 63 / 18 / 12 / 57 | `results/readme_numbers.md` → 2×2 paired agreement |
| cost baseline $0.1581, main $0.4273 | `results/readme_numbers.md` → Cost and latency |
| mean latency baseline 32.6 s, main 134.1 s | `results/readme_numbers.md` → Cost and latency |
| 10-call cap hits baseline 4, main 14 | `results/readme_numbers.md` → Tool-call cap hits |
| no main tool call blocked by check_sql or the cost gate (accuracy run) | `results/readme_numbers.md` → Tool-call cap hits (blocked by check_sql/cost gate = none) |
| dry run 82 rows, 0 mismatches, 3 runs, no row changed | `results/readme_numbers.md` → Controlled attack dry run |
| Model-driven attacks show no like-for-like evidence that QueryGuard reduced harm | `results/phase6_report_6b.md` §4b / Summary ("not like-for-like … no evidence that main beat baseline") |
| the model's final SQL text is not checked by the server | `results/phase6_report_6b.md` §6 ("The model's final SQL text is not guarded") |

## Quick start (factual claims only)

| claim | source |
|---|---|
| the demo DB is synthetic (fictional bookstore), not BIRD data | `db/demo/01_schema.sql` (header); `db/demo/02_seed.sql` |
| runs under its own compose project; never touches a BIRD stack; dev-only placeholders | `docker-compose.demo.yml` (header comments) |
| stdio is the only transport the server supports | `src/queryguard/server.py` (`mcp.run` stdio); `docs/CLIENT_CONFIG.md` |
| setup_demo reuses scripts/create_reader.sql | `scripts/setup_demo.py`; `scripts/create_reader.sql` |

## Tools (one line each)

| claim | source |
|---|---|
| list_databases / list_tables / describe_table / find_columns / validate_sql / execute_query descriptions | `src/queryguard/server.py` (each tool's `description=`); 100-row cap + 5 s in `execute_query` description |

## Guard layers (code facts)

| claim | source |
|---|---|
| check_sql: one statement, allow-listed root, no write nodes, R1–R8, function allow-list, name regex `^[a-z_][a-z0-9_]*$`, LIMIT capped, re-rendered; only safe_sql executed | `src/queryguard/safety.py`; `src/queryguard/executor.py` (`run_checked` executes only `check.safe_sql`); `CLAUDE.md` Safety layers / Decisions |
| read-only role qg_reader: login-only, non-superuser, SELECT/USAGE, no CREATE | `scripts/create_reader.sql` (steps 2–4); confirmed by `scripts/verify_setup.py` output |
| REVOKE EXECUTE … FROM PUBLIC on pg_logical_emit_message, txid_current, pg_current_xact_id, setseed | `scripts/create_reader.sql` (step 7, lines 71–82); `results/phase7_checks.txt` (verify_setup list) |
| 5 s per query set by the server | `src/queryguard/executor.py` (`STATEMENT_TIMEOUT = "5s"`, `SET LOCAL` in `_execute`) |
| 30 s role default for direct connections | `scripts/create_reader.sql` (line 63); `results/phase7_part2_checks.md` §0 / `phase7_checks.txt` (observed '30s') |
| row cap 100 | `src/queryguard/executor.py` (`DEFAULT_MAX_ROWS = 100`) |
| cost gate limit 460,000 | `src/queryguard/executor.py` (`EXPLAIN_COST_LIMIT = 460_000`); `results/phase7_checks.txt` (smoke: cost_limit=460000) |
| PII masking by column name / email-shaped value; best-effort | `src/queryguard/pii.py`; `CLAUDE.md` (PII decision) |
| error redaction before leaving the server | `src/queryguard/executor.py` (`_redact_db_error`); `CLAUDE.md` (error-text decision) |
| one JSON audit line per execute_query (no secrets) | `src/queryguard/executor.py` (`_audit`) |

## Evaluation design

| claim | source |
|---|---|
| 11 databases, 75 tables, 3,898,114 rows | `results/readme_numbers.md` → Repository/integrity (total rows across 11 databases) |
| sample = 150 questions (`eval/sample_150.json`) | `results/readme_numbers.md` (Primary accuracy denominators); `eval/PREREGISTRATION.md` |
| model Groq openai/gpt-oss-120b, reasoning_effort medium, temperature 0, 2048 tokens, prompt a16401d6 | `eval/PREREGISTRATION.md` (Settings); prompt version also `results/readme_numbers.md` → Repository/integrity |
| MAX_TOOL_CALLS = 10 | `results/readme_numbers.md` → Tool-call cap hits; `eval/PREREGISTRATION.md` |
| two conditions (baseline has full schema in its tool description; main discovers it) | `eval/PREREGISTRATION.md` |
| preregistered analysis: margin 10, paired bootstrap 10,000 seed 1234, exact McNemar, Wilson | `eval/PREREGISTRATION.md`; values also in `results/readme_numbers.md` → Inference |
| re-run rule (outage-only; no post-run code change) | `eval/PREREGISTRATION.md` (Re-run rule) |

## Attack results (counts + definitions)

| claim | source |
|---|---|
| relabelled counts table (reader/plain × baseline/main) | `results/readme_numbers.md` → Attack counts, relabelled categories |
| official classifier counts table | `results/readme_numbers.md` → Attack counts, official table |
| category definitions (harm, harm_flagged_discovery_only, blocked, contained, validated_only, wrote_sql_not_executed, text_refusal, other) | `results/attack_summary_6b.md` (label rules); `eval/attacks_e2e.json` (harm rule); `CLAUDE.md` (attack harm rule) |
| 8 main blocked cells (reader/main 5, plain/main 3); per-layer split in the report | `results/readme_numbers.md` (blocked column) → per-layer in `results/phase6_report_6b.md` §4b |
| dry run 82 rows / 0 mismatches / 3 runs | `results/readme_numbers.md` → Controlled attack dry run |

## Error analysis

| claim | source |
|---|---|
| Pending: blind labelling of wrong answers (owner) | `results/phase6_report_6b.md` §3 ("Pending: human blind labels not yet done") |

## Limitations

| claim | source |
|---|---|
| one model, one sample (n=150), one run per question | `eval/PREREGISTRATION.md`; `results/phase6_report_6b.md` §6 |
| non-determinism: 2 of 12 repeated attack cells changed | `results/attacks_reader_pii_6b.json` vs `results/attacks_reader_pii_6b_diag.json` (12 cells; 2 differ — verified: pii_admin_emails_direct/baseline, pii_email_substr_bypass/main); qualitatively in `results/phase6_report_6b.md` §6 |
| masking exercised only in scripted runs; alias, substr, to_jsonb bypasses leak | `results/phase6_report_6b.md` §6 / Summary; `CLAUDE.md` (PII bypasses) |
| the final SQL text is not guarded | `results/phase6_report_6b.md` §6 |
| an accuracy cost cannot be ruled out | `results/phase6_report_6b.md` §1/§6 (CI spans the −10 margin → inconclusive) |
| free-tier accounts switched, not logged per question | `results/RUN_LOG.md` (retrospective entries) |
| no pre-run code freeze; hashes and git created after the runs | `results/RUN_LOG.md` (retrospective); `results/CHECKSUMS_6B.txt` (header); `results/CODE_HASHES_PHASE7_START.txt` (header) |
| classifier partial-match false alarms on discovery tools | `results/phase6_report_6b.md` §6; `results/attack_summary_6b.md` §(d) |
| stdio only: no authentication, no hosted deploy | `src/queryguard/server.py` (stdio; no auth in code); `docs/CLIENT_CONFIG.md` |
| CI's unit job runs 460 tests (no DB; BIRD_DBS set to the 11 db_id names only) + demo integration; DB/BIRD tests (whole modules test_reader_role/test_reader_revokes/test_allowlist_catalog/test_bird_data/test_safety_gold/test_schema_tools + specific tests in test_baseline_server/test_executor/test_harness/test_main_server) are marked `slow` and run in integration or locally, as does the dry run | `.github/workflows/ci.yml` (unit job `pytest -m "not slow"`, `BIRD_DBS` = 11 names); `tests/*.py` (slow markers); `results/phase7_checks.txt` (marker-refinement clean-copy run: 460 passed, 90 deselected) |

## Reproduce / Repo map / Licence

| claim | source |
|---|---|
| evaluation uses BIRD Mini-Dev; do not redistribute; licence = verify | `CLAUDE.md` (data/ description); licence intentionally unstated and marked "verify" |
| load/split/create_reader commands | `scripts/load_bird.py`, `scripts/split_bird.py`, `scripts/create_reader.py`; `README.md` quick start |
| eval + analyze commands | `eval/harness.py`, `eval/analyze_phase6.py`; `results/phase6_report_6b.md` (analyze command); `results/RUNBOOK_6B.md` |
| repository map (folders) | directory listing (src, eval, scripts, tests, db, docs, results, data, .github) |
| Licence: TODO(owner) | not a factual claim; owner to fill in |

## Part 3 — new / changed README sentences (after review)

| claim (new or changed) | source |
|---|---|
| the 6 pii cells in the reader rows come from the diagnostic pii run; the official first pii run (no transcript) is shown separately | `results/attack_summary_6b.md` (Inputs list names the pii diag run; first run "has no transcript… reported separately"); `results/phase6_report_6b.md` §4b |
| first reader/pii run (official, no transcript): baseline harm 5 / blocked 0 / contained 0 / model_refused 1; main 1 / 0 / 0 / 5 | `results/phase6_report_6b.md` §4b (first reader/pii run table); `results/attacks_reader_pii_6b.json` |
| the single main harm in the first run is a describe_table flag with no transcript and is unverified | `results/phase6_report_6b.md` §4b |
| main blocked-layer table: check_sql 4, cost gate 1, statement_timeout 1, permission denied 2 | `results/phase6_report_6b.md` §4b (layer table + reworded paragraph) |
| PostgreSQL enforces the timeout; the 5 s value is set by the application in both conditions (eval/baseline_server.py and src/queryguard/executor.py each `SET LOCAL statement_timeout='5s'`); baseline applies the same 5 s | `results/phase6_report_6b.md` §4b (reworded); `CLAUDE.md` (baseline-protections decision); `src/queryguard/executor.py`; `eval/baseline_server.py` (per CLAUDE.md) |
| permission-denied blocks come from the qg_reader role and its REVOKEs, identical in both conditions; only check_sql (4) and the cost gate (1) are main's own code | `results/phase6_report_6b.md` §4b (reworded); `scripts/create_reader.sql` |
| the attacks both conditions ran through the pipeline (pg_authid reader, pg_ls_dir reader and plain, unbounded recursion reader) produced no harm in either condition | `results/phase6_report_6b.md` §4b ("Attacks appearing as harm/blocked in both conditions… None produced harm in either condition") |
| re-run rule note: two harness fixes during the baseline run changed error handling and stop logic only, not prompt/tools/extractor/scoring | `results/RUN_LOG.md` (2026-10-10 harness fix #1; 2026-10-11 harness fix #2); `results/phase6_report_6b.md` §5 |
| masking's effect on accuracy was not measured: no sample_150 question has the pii_masked tag (0 removed) | `results/phase6_report_6b.md` §2 (tag-exclusion table, pii_masked 0 removed) + §6 (new limitation) |
| two harness fixes during the baseline run (after 16 records): error handling + stop logic, not scoring | `results/RUN_LOG.md` (2026-10-10, 2026-10-11); `results/phase6_report_6b.md` §5 |
| CI builds the Docker image but does not run it; the image was tested locally only | `.github/workflows/ci.yml` (docker-build job: `docker build`, no run); `results/phase7_checks.txt` (local demo run) |
| test count: 550 total (all 11 BIRD db_id names) = 460 in CI unit job + 90 `slow`; the CI unit job and the full environment both set BIRD_DBS to the 11 names, so both collect 550 (full env runs all 550 with the databases up; CI runs the 460 non-slow and deselects the 90) | `results/phase7_checks.txt` (marker-refinement accounting: `550 tests collected` = 460 + 90; clean-copy run `460 passed, 90 deselected` with BIRD_DBS = 11 names and no database) |

## Part 4 — new / changed README + DEMO_SCRIPT sentences (after review)

| claim (new or changed) | source |
|---|---|
| intro: the evaluation compares the guarded discovery-based server (6 tools) with a minimal baseline — one `run_sql` tool + full schema, same read-only `qg_reader` role, same 5 s timeout and 100-row cap, no check_sql/cost gate/masking/redaction | `eval/baseline_server.py` (`run_sql` with schema in its description; `STATEMENT_TIMEOUT="5s"` L28; `ROW_CAP=100` L27; read-only role via `get_pool`+`transaction(readonly=True)` L126; no safety checks per module docstring); `eval/PREREGISTRATION.md` (two conditions) |
| Evaluation design: baseline described exactly as the intro | same as above |
| PII masking: strong tokens email/phone/password/aadhaar/ssn (substring); weak tokens pan/mobile/token/secret (whole word); email value regex `^[^@\s]+@[^@\s]+\.[^@\s]+$`; personal-name columns not masked | `src/queryguard/pii.py` (`_STRONG_TOKENS`, `_WEAK_TOKENS`, `_EMAIL_RE`, `is_pii_column`); verified in `results/phase7_part4_checks.md` §0b |
| validate_sql: `ok: true` means the query passes the rules; check `within_cost_limit` separately | `src/queryguard/schema.py` `validate_sql` (returns both `ok` and `within_cost_limit`); `results/phase6_report_6b.md` §6 |
| dry-run subsection: each attack's SQL sent directly to both conditions, no model; counts per setting×condition (reader baseline 18/7/5; reader main 21/3/6; plain baseline 5/4/2; plain main 10/0/1); 82 rows / 0 mismatches / 3 runs | `results/attack_dryrun.md` (counts **derived** by tallying its 82 rows — shown in `results/phase7_part4_checks.md` §0c); `eval/dryrun_attacks.py` (sends SQL to both conditions, no model) |
| Limitations: the two conditions differ in two ways at once (guards, and discovery vs given schema), so a difference can't be assigned to the guards alone; in the accuracy run the guards blocked no call and no question touched a masked column | `results/phase6_report_6b.md` §6 (design confound) + §2 (no main call blocked by check_sql/cost gate); `results/readme_numbers.md` (blocked by check_sql/cost gate = none); `eval/tags.json`/report §2 (pii_masked 0 in sample_150) |
| Limitations: "An accuracy cost of the **main server design** cannot be ruled out" (was "guard layers") | `results/phase6_report_6b.md` §1/§6 (CI spans the margin) |
| Limitations: personal names (e.g. `last_name`) are not masked; `describe_table` shows their sample values | `src/queryguard/pii.py` (names not PII); `src/queryguard/server.py`/`schema.py` (`describe_table` withholds sample values only for PII columns); `results/phase7_part4_checks.md` §0b |
| Limitations: tool parameter descriptions are empty in the tool schema | `results/phase6_report_6b.md` §6; `results/main_tool_text.txt` |
| Reproduce: setup scripts in path form (not `-m`) because of a bare `_dockerutil` import; eval commands in `-m` form | `results/phase7_part4_checks.md` §0e (`-m scripts.*` → ModuleNotFoundError; `-m eval.*` works); `CLAUDE.md` (eval/inspect.py shadow) |
| Reproduce: use new output files `results/baseline_repro.jsonl` / `results/main_repro.jsonl` (because `--resume` on the committed `_6b` files skips all 150) | `eval/PREREGISTRATION.md` / `eval/harness.py` (`--resume` skips done ids) |
| Reproduce: approximate prompt-token need (derived = 150 × report §2 per-question mean): baseline ≈ 150 × 3894.4 = 584,160; main ≈ 150 × 14404.3 = 2,160,645 | `results/phase6_report_6b.md` §2 (mean prompt tokens baseline 3894.4, main 14404.3); product is derived and labelled |
| Reproduce: the full pytest suite needs the BIRD databases (every test that connects to a DB or reads the BIRD dataset is marked `slow`); CI's unit job runs `pytest -m "not slow"`, the 460 that need neither DB nor BIRD data | `tests/conftest.py` (`reader_conn` connects); `pyproject.toml` (`slow` marker); `tests/*.py` (slow markers); `.github/workflows/ci.yml` (`-m "not slow"`); `results/phase7_checks.txt` (460 passed) |
| DEMO_SCRIPT on-screen note: demo uses a direct MCP client (e.g. MCP Inspector), not an AI assistant; shows the server blocking, not an assistant declining | design choice; `scripts/smoke_mcp.py` (direct client exercising the same calls) |
| DEMO_SCRIPT (a): "common PII columns are masked (best-effort)" (was "PII is masked") | `src/queryguard/pii.py`; `CLAUDE.md` (masking is best-effort) |
| DEMO_SCRIPT (c): MCP Inspector launch command; marked "not tested here" (GUI); same direct calls verified by smoke_mcp | `docs/CLIENT_CONFIG.md` (stdio launch); `scripts/smoke_mcp.py` |
| DEMO_SCRIPT (e): ~2.7× more cost and ~4× slower (derived: $0.4273/$0.1581 ≈ 2.7×; 134.1 s / 32.6 s ≈ 4.1×) | `results/readme_numbers.md` (cost/latency); products derived and labelled |

## Part 5 — new / changed README sentences (after review)

| claim (new or changed) | source |
|---|---|
| Result-in-brief: scripted dry-run harm cells — baseline 11 (reader 7, plain 4), main 3 (reader 3, plain 0) | `results/attack_dryrun.md` (**derived** by tallying rows with outcome `harm`: baseline reader 7 + plain 4 = 11; main reader 3 + plain 0 = 3) |
| main's 3 harm cells are `pii_phone_alias_bypass`, `pii_email_substr_bypass`, `pii_to_jsonb_bypass` (reader setting) | `results/attack_dryrun.md` (the three `main`/`reader` rows whose outcome is `harm`) |
| settings: reader = `qg_reader` (read-only; login-only, non-superuser, SELECT/USAGE, no CREATE, `default_transaction_read_only=on`, EXECUTE revoked from PUBLIC on 4 functions) | `scripts/create_reader.sql` (role attributes, grants, `default_transaction_read_only=on` L62, REVOKE loop L71–82) |
| settings: plain = `qg_plain` — unhardened control; lacks `default_transaction_read_only`, lacks `statement_timeout`, EXECUTE granted back on the 4 functions; CONNECT+USAGE+SELECT otherwise | `scripts/create_plain.sql` (header + grants; "Still intentionally omitted" note L79–81; EXECUTE grant-back L66–77) |
| plain exists to isolate what the qg_reader hardening buys; created for the runs and dropped afterwards | `scripts/create_plain.sql` (purpose header); `scripts/drop_plain.*`; `CLAUDE.md` (qg_plain decision); `results/RUN_LOG.md` (qg_plain dropped) |
| dry-run table column labels: each outcome column is a count of cells; `n` = number of attack cells | `results/attack_dryrun.md` (one row per attack×condition; counts tallied) |
| Limitations: the 30 attacks were author-written and the guards were developed with these cases in view; the dry run shows they stop these attacks, not unknown ones | `eval/attacks_e2e.json` (30 author-written attacks); design fact (guards and attack set developed together) |

## Part 6 — new / changed README sentences (after review)

| claim (new or changed) | source |
|---|---|
| intro "pre-specified evaluation" / "Pre-specified analysis" (was "preregistered") | wording change only (B3); no new claim — the analysis spec is still `eval/PREREGISTRATION.md` |
| Evaluation design: verbatim "Re-run rule" block (outage-only re-runs; wrong/no_sql never re-run; rate-limit restart = provider stop; no prompt/tool/extractor/scoring change; tool_use_failed 400 = model failure scored no_sql; two harness fixes before the freeze tag) | `eval/PREREGISTRATION.md` lines 71–98 (quoted verbatim); intent line 4 |
| note: `output_parse_failed` is not mentioned in the preregistration, only in RUN_LOG | `eval/PREREGISTRATION.md` (grep: 0 matches for `output_parse_failed`); `results/RUN_LOG.md` (2026-10-14 output_parse_failed entry) |
| Limitations (verbatim B2): PREREGISTRATION.md last modified 2026-10-11 00:23 IST, during the baseline run (16/150 baseline done, main not started), one minute after harness fix #2; no git history; RUN_LOG records no analysis before then | mtime `2026-10-10T18:53:08Z` = 2026-10-11 00:23 IST (`results/CHECKSUMS_6B.txt`, `results/CODE_HASHES_PHASE7_START.txt`); fix #2 logged 2026-10-11 00:22 IST and "16 baseline records written before both fixes", main run starts after baseline completes (`results/RUN_LOG.md`); analysis dated 2026-10-14 (`results/RUN_LOG.md`, `results/phase6_report_6b.md`) |
| blocked-layer paragraph: both permission-denied blocks are `sa_pg_authid` (one per setting); only superusers read `pg_authid`, so the block is the same for both roles and conditions | `results/phase6_report_6b.md` §4b (permission-denied = 2 cells, both `sa_pg_authid`, reader + plain); `results/attack_dryrun.md` (`sa_pg_authid` → blocked, "permission denied (database)", both conditions); PostgreSQL default (`pg_authid` readable only by superusers) |
| Reproduce: "200,000 tokens/day (the limit at the time of the runs)" | `CLAUDE.md` (Groq free tier 200,000 tokens/day); "at the time of the runs" marks it as the then-current limit |
