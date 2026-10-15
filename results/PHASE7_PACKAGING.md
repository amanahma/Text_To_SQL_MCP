# Phase 7 Part 1 — packaging report

Scope: A (inventory), B (Docker), C (CI). Windows 11 + PowerShell + Docker Desktop
(WSL2). No git, no API/Groq, no eval runs. Docker was healthy (`docker info` →
ServerVersion 29.8.1, daemon up), so **Part B and the Docker CI job were not skipped**.

Every "passed/tested" claim below has its command + real output in
`results/phase7_checks.txt`. Deviations are in `results/PHASE7_BLOCKERS.md`.

## Files created (new only — no protected file was edited)

| file | part | purpose |
|---|---|---|
| `results/CODE_HASHES_PHASE7_START.txt` | A1 | sha256/size/mtime of src/queryguard, eval, scripts, tests, pyproject.toml, uv.lock (78 files) |
| `results/write_ddl_file_comparison.md` | A2 | compares the two write_ddl JSONs (byte-identical; only mtime differs) |
| `results/readme_numbers.md` | A3 | every README number → value + source file/field |
| `Dockerfile` | B1 | Python 3.12 server image, non-root (uid 10001), no secrets, no .env |
| `.dockerignore` | B1 | excludes .env, results/transcripts, zips, caches, __pycache__, data/, etc. |
| `docker-compose.demo.yml` | B2 | postgres:16 + server; loopback-only; dev placeholders; reuses create_reader.sql |
| `db/demo/01_schema.sql`, `db/demo/02_seed.sql` | B3 | synthetic bookstore DB (3 tables, 270 rows, fake email+phone PII) |
| `scripts/setup_demo.py` | B2 | wraps the reused create_reader.sql to set up the demo DB (compose or direct psql) |
| `scripts/verify_setup.py` | B4 | asserts the reader role is locked down; one line per check; non-zero on failure |
| `scripts/smoke_mcp.py` | B5 | MCP stdio smoke test (6 tools, SELECT, DROP block, pg_ls_dir block, cost gate, masking) |
| `docs/CLIENT_CONFIG.md` | B6 | client config for the stdio transport (the only transport the server supports) |
| `.github/workflows/ci.yml` | C1 | 3 jobs: unit (no DB), integration (demo DB + MCP smoke), docker build (no push) |
| `results/phase7_checks.txt` | all | command + output log |
| `results/PHASE7_BLOCKERS.md` | all | documented deviations |

## Part A results

- **A1** — `CODE_HASHES_PHASE7_START.txt`, 78 files, header states it was computed after
  all Phase 6B runs and is not a pre-run freeze. `__pycache__`/`.pyc` excluded (noted in file).
- **A2** — the two files are **byte-for-byte identical** (equal sha256
  `c89ac1ab…fad59`); the only difference is mtime (`04:55:53Z` vs `06:20:38Z`). Every
  attack cell/outcome/field matches. Neither file renamed or deleted.
- **A3** — `readme_numbers.md` has one table per metric group, each value tagged with its
  source file and field/line. No new statistics computed. One value marked **MISSING** (a
  precomputed fingerprint-equality field inside the fingerprint JSON — equality is asserted
  in prose only). Test count (550) and non-slow count (511) measured here.

## Part B results — all PASS

Full sequence run against the committed (ruff-formatted) files; all exit 0
(`results/phase7_checks.txt` §B7):

1. `build` → image `queryguard-demo-server` built.
2. `up -d` → db healthy, server started.
3. `setup_demo` → schema ok, seed ok, reader role (create_reader.sql) ok.
4. `verify_setup` → **all checks passed**: not superuser; INSERT/UPDATE/DELETE/CREATE
   denied (read-only transaction); EXECUTE denied on all overloads of the 4 revoked
   functions (list read + printed from create_reader.sql); statement_timeout bounded cap.
5. `smoke_mcp` → **all checks passed**: exactly 6 tools; list_tables (3 tables); SELECT
   returns 5 rows; DROP TABLE blocked (R4); `pg_ls_dir('.')` blocked (R6);
   `generate_series(1,1e9)` over cost limit (within_cost_limit=False, est 12,500,000.01 vs
   limit 460,000); fake-email column masked (`['***',…]`) — labelled "basic masking path
   only; known bypasses (alias, substr, to_jsonb) are NOT tested here".
6. `down -v` → volume + network removed.

One deviation (not a failure): `verify_setup` reports the role-level `statement_timeout`
as `30s` (what `create_reader.sql` sets) and notes the executor narrows it to `5s` per
query; see PHASE7_BLOCKERS.md §1. The compose file is `docker-compose.demo.yml`, not an
overwrite of the owner's `docker-compose.yml`; see PHASE7_BLOCKERS.md §2.

## Part C results

- **C1** — `.github/workflows/ci.yml`: `permissions: contents: read`; no secrets; no Groq/eval.
  Jobs: (1) `unit` runs `pytest -m "not slow"` with **no Postgres service** (the 511 non-slow
  tests pass without a DB; the 39 slow tests need BIRD data and are excluded); (2) `integration`
  uses a `postgres:16` service, loads the demo DB + role setup (`setup_demo` in direct-psql
  mode), then runs `verify_setup.py` and `smoke_mcp.py`; (3) `docker-build` builds the image,
  no push. Action versions pinned to release tags (note in-file: repin to SHAs for stricter
  hardening).
- **C2** — YAML parses (jobs unit/integration/docker-build; permissions contents:read);
  `actionlint` (via its Docker image) reported **no problems** (exit 0). **STATED PLAINLY:
  the workflow has NOT RUN on GitHub yet** — only local YAML validation + actionlint.
- **C3** (report only, nothing changed) — see next section.

## C3 — can the attack dry-run run against the demo fixture?

**No — not unchanged.** `eval/dryrun_attacks.py` is wired to the BIRD databases in three
ways the synthetic demo fixture does not satisfy, and the prompt forbids adapting any
predicate:

1. **Every one of the 82 rows needs a BIRD database by name.** Each cell connects to
   `attack["db_id"]` — one of `california_schools`, `european_football_2`, `card_games`,
   `student_club`. The demo provides only `queryguard_demo`. Unchanged, all 41 cells × 2
   conditions = **82 rows** fail at connect / unknown-database. The runner also hardcodes
   `CONTAINER = "queryguard-mcp-db-1"` and pre-reads real PII values as `qg_reader`
   (`read_pii_values`) from BIRD tables.
2. **The 22 plain-setting rows also need the `qg_plain` role** (`scripts/create_plain.py`),
   which the demo does not create.
3. **30 rows additionally need specific BIRD tables/columns**, not just the database name.

**Rows that need a BIRD _table_ (beyond the db name): 30** = these **15 reader attacks ×
2 conditions** (the scripted SQL / describe_table references a BIRD table):

| attack | BIRD table (db) |
|---|---|
| w_drop_table | schools (california_schools) |
| w_delete_rows | frpm (california_schools) |
| w_update_rows | schools (california_schools) |
| pii_email_error_text | schools (california_schools) |
| pii_admin_emails_direct | schools (california_schools) |
| pii_phone_alias_bypass | schools (california_schools) |
| pii_email_substr_bypass | schools (california_schools) |
| pii_to_jsonb_bypass | schools (california_schools) |
| pii_member_emails | member (student_club) |
| r_cross_join_players | player (european_football_2) |
| r_heavy_hash_sort | cards (card_games) |
| pt_multi_statement | schools (california_schools) |
| pt_comment_hidden | schools (california_schools) |
| hint_inject_drop | schools (california_schools) |
| hint_inject_pii | member (student_club) |

**Rows that reference NO BIRD table (functions / system catalogs / generate_series
only): 52** = 15 reader attacks × 2 (`se_wal_emit_txn`, `se_wal_emit_nontxn`,
`se_txid_current`, `se_pg_current_xact_id`, `se_disable_timeout`, `r_huge_generate_series`,
`r_unbounded_recursion`, `r_order_by_random`, `sa_pg_authid`, `sa_pg_read_file`,
`sa_copy_program`, `sa_lo_import`, `sa_pg_ls_dir`, `sa_catalog_pg_settings`,
`pt_unicode_escape_func`) + all 11 plain attacks × 2. These **still need the BIRD database
to exist by name** to connect (point 1), so they are not demo-runnable either.

**Bottom line:** the dry-run is **not added to CI** (it does not run unchanged against the
demo fixture). Running it would need the BIRD databases + the `qg_plain` role, i.e. the
owner's full BIRD stack, not the packaging demo.

## NOT RUN

- CI workflow on GitHub Actions (local YAML + actionlint only).
- The 39 `slow` BIRD-backed tests (need the 955 MB BIRD databases; only the 511 non-slow
  tests were executed).
- The attack dry-run against the demo fixture (report-only by design; see C3).
- Any eval / Groq / accuracy run (forbidden).

## Blockers / deviations

See `results/PHASE7_BLOCKERS.md`: (1) `verify_setup` reports `statement_timeout = 30s`
(role default) rather than asserting `5s`, because the 5s is an executor per-query cap and
`create_reader.sql` must be reused unchanged; (2) a separate `docker-compose.demo.yml` was
added instead of overwriting the owner's `docker-compose.yml`, to avoid breaking the BIRD +
eval + attack workflow. No protected file was edited. README not written (per the prompt).
