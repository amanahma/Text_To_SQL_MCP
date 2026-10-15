# Phase 7 Part 2 — STEP 0 checks

Date 2026-10-14. No git, no API, no `.env` read, no protected-file edits.

## 0.1 Re-hash of the Part 1 snapshot

Recomputed sha256 for all **78** files in `results/CODE_HASHES_PHASE7_START.txt`.

- **Changed files: 0.** No file in the snapshot changed.
- **Now-absent files: 0.**
- **Created since (within the hashed scope src/queryguard, eval, scripts, tests,
  pyproject, uv.lock): 3**, all in `scripts/` and all new in Part 1:
  `scripts/setup_demo.py`, `scripts/smoke_mcp.py`, `scripts/verify_setup.py`.

Expected invariant **holds**: no change in `src/`, `eval/` or `tests/` (nor in
`pyproject.toml`/`uv.lock`). Not stopping — nothing to report as a violation.

(Other new files created in Part 1 live outside the hashed scope and so are not in
the snapshot: `Dockerfile`, `.dockerignore`, `docker-compose.demo.yml`, `db/demo/*.sql`,
`docs/CLIENT_CONFIG.md`, `.github/workflows/ci.yml`, and the named `results/` outputs.)

## 0.2 The pytest "slow" marker — pre-existing, not added in Part 1

**Pre-existing.** It is defined in `pyproject.toml`, lines 36–38:

```
36      markers = [
37          "slow: tests that need the database (still run by default; deselect with -m 'not slow').",
38      ]
```

`pyproject.toml` is in the 0.1 snapshot and its sha256 is **unchanged**, so the marker
was present before Part 1 and was not added or edited by it. Part 1 only *used* the
marker (CI runs `pytest -m "not slow"`); it did not define it.

## 0.3 Part 1 commands that failed before passing

Exactly **one** command failed-then-passed, and it was a formatting check on the new
files — no behavioural check was touched:

| command | failure | fix |
|---|---|---|
| `uv run ruff format --check scripts/{setup_demo,verify_setup,smoke_mcp}.py` | exit 1, "3 files would be reformatted" (line-wrapping of long dict/list literals and one long string) | ran `uv run ruff format` on the three files (whitespace/line-wrapping only), then re-checked → "3 files already formatted"; `ruff check` stayed "All checks passed!" |

Everything else passed on first run: `docker compose build`, `up -d`, `setup_demo`,
`verify_setup`, `smoke_mcp`, `down -v`, `pytest -m "not slow"` (511 passed), YAML parse,
`actionlint` (exit 0).

**One pre-run edit to `verify_setup.py` (made before it was ever executed), for
completeness:** the UPDATE write-probe was changed from `SET ctid = ctid` to
`SET <discovered_column> = <discovered_column>`. Reason: `ctid` is a system column and
assigning to it raises "cannot assign to system column" — a *different* error than the
read-only denial the check is meant to prove. The change makes the probe a real UPDATE
that is blocked by the read-only transaction. This **strengthened** the check (it now
tests the intended denial path); it did not weaken it.

**Explicit statement:** **no assertion in `smoke_mcp.py` or `verify_setup.py` was
loosened, removed or skipped.** All checks in both scripts are the originals (the
`ruff format` pass changed only whitespace; the one `verify_setup.py` edit above
strengthened a probe). Both scripts ran green with every check asserting PASS.

## 0.4 write_ddl file + transcript mtimes

| file | mtime (UTC) | mtime (IST, UTC+5:30) |
|---|---|---|
| `results/attacks_reader_write_ddl.json` (no suffix) | 2026-10-14T04:55:53Z | 10:25:53 |
| `results/attacks_reader_write_ddl_6b.json` (official) | 2026-10-14T06:20:38Z | 11:50:38 |
| `results/transcripts/attacks_reader_write_ddl_6b_transcripts.json` | 2026-10-14T06:20:38Z | 11:50:38 |

The two JSONs are byte-identical (same sha256 `c89ac1ab…fad59`), confirmed in Part 1.

**Does this fit the batch-time line?** The existing RUN_LOG retrospective places the
model-driven batches (write_ddl among them) at **11:35–11:57 IST**. The **official**
write_ddl run — both `attacks_reader_write_ddl_6b.json` and its transcript — is at
**11:50:38 IST**, which **fits** that window. The no-suffix
`attacks_reader_write_ddl.json` at **10:25:53 IST** predates the batch, consistent with
the existing retrospective that its "origin was not established" (it is not the official
run). Because the official write_ddl time fits the batch window, the batch-time line is
**not** contradicted for write_ddl, so the optional caveat line is **not** added.

RUN_LOG line appended (see `results/RUN_LOG.md`): see 0.4 append below.

## 0.5 The MISSING value in readme_numbers.md

**MISSING:** a precomputed *fingerprint-equality boolean field inside the fingerprint
JSON*. `readme_numbers.md` reports the before/after row-count equality (asserted in prose
as "equal" in `results/phase6_report_6b.md` §5, and "True" for the dry run in
`results/attack_dryrun.md`), but there is no stored equality flag in the JSON itself.

**Where it would come from (not computed here):** comparing the `row_counts` objects in
`results/fingerprint_6b_before.json` and `results/fingerprint_6b_after.json` (the files
differ only in their `taken_at` timestamp). Not computed, per the rules.

## 0.6 Demo Postgres port vs the BIRD container

- Demo stack (`docker-compose.demo.yml`) publishes PostgreSQL on
  **`127.0.0.1:55432`** (host) → 5432 (container), via `${DEMO_DB_PORT:-55432}`.
- BIRD stack (`docker-compose.yml`) publishes on **`127.0.0.1:${DB_PORT}`**, and
  `.env.example` sets `DB_PORT=5433`.
- **No clash:** 55432 ≠ 5433. The demo also runs under a separate compose project
  (`-p queryguard-demo`) with its own container/volume names, so nothing collides with
  `queryguard-mcp-db-1` / `queryguard-mcp_pgdata`.
