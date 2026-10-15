# Phase 7 Part 4 — STEP 0 checks

Date 2026-10-14. No git, no API, no `.env` read. No edits to src/, eval/, tests,
PREREGISTRATION.md or create_reader.sql. No number changed; derived numbers say so.

## 0a. baseline_server.py — timeout, row cap, role (exact lines)

From `eval/baseline_server.py`:

```
27  ROW_CAP = 100
28  STATEMENT_TIMEOUT = "5s"
...
126         async with pool.acquire() as conn, conn.transaction(readonly=True):
127             await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
...
133                 if len(rows) >= ROW_CAP:
134                     break
```

- **statement_timeout:** set by the baseline itself — line 28 defines `"5s"`, line 127
  issues `SET LOCAL statement_timeout = '5s'` in the `run_sql` transaction.
- **row cap:** `ROW_CAP = 100` (line 27), enforced by the `len(rows) >= ROW_CAP` break
  (lines 133–134).
- **role:** the connection comes from `queryguard.db.get_pool` (the `qg_reader` reader role),
  inside an explicit read-only transaction (`conn.transaction(readonly=True)`, line 126).

**Conclusion: baseline DOES set 5 s itself.** The README layer-table wording ("the 5 s value
is set by the application in both conditions … `eval/baseline_server.py` and … the main
executor") is accurate; **no fix needed**.

## 0b. pii.py — tokens, email pattern, and names

From `src/queryguard/pii.py`:

- **Strong tokens** (substring match against the whole lower-cased column name):
  `email`, `phone`, `password`, `aadhaar`, `ssn`.
- **Weak tokens** (whole-word match only): `pan`, `mobile`, `token`, `secret`.
- **Email value pattern:** `^[^@\s]+@[^@\s]+\.[^@\s]+$` (anchored; a bare `local@domain.tld`).

**Columns named `name`, `first_name`, `last_name`: NOT masked.** None contains a strong
token as a substring, and splitting on non-alphanumerics gives words
(`name`; `first`,`name`; `last`,`name`) none of which is a weak token.

**describe_table sample values for them: YES, shown.** `describe_table` withholds sample
values only for PII columns (`is_pii_column`); since these names are not PII columns, their
sample values are shown. → **1e applies** (add the names limitation to the README).

## 0c. attack_dryrun.md — outcome counts per setting × condition (counts only)

Using the file's own outcome labels (`blocked`, `harm`, `contained`), 82 data rows:

| setting | condition | blocked | harm | contained | n |
|---|---|---|---|---|---|
| reader | baseline | 18 | 7 | 5 | 30 |
| reader | main | 21 | 3 | 6 | 30 |
| plain | baseline | 5 | 4 | 2 | 11 |
| plain | main | 10 | 0 | 1 | 11 |

## 0d. README quick start, run verbatim from a clean temp copy

Temp copy: `…\Temp\repo_clean`, made without `.env`, `data/` or `results/transcripts`
(also without `.venv`, so `uv` builds a fresh environment — a realistic fresh checkout). The
prebuilt `queryguard-demo-server` image was removed first so `up` had to build from the copy.
Ran in a fresh PowerShell with `DEMO_DB` and `BIRD_DBS` unset (confirmed `DEMO_DB=[]
BIRD_DBS=[]`), the five quick-start lines exactly as written.

Result: **all steps succeeded and every step targeted `queryguard_demo` only.**

- `up -d` built the image from the temp copy and started db (healthy) + server.
- `uv run python -m scripts.setup_demo` → `schema: ok / seed: ok / reader role: ok / Done`
  on `queryguard_demo` (`uv` first built a fresh `.venv`, "Installed 79 packages").
- `verify_setup` → "RESULT: all checks passed." (on `queryguard_demo`).
- `smoke_mcp` → "RESULT: all checks passed." (6 tools; R4/R6 blocks; cost gate; masked email).
- `down -v` → volume + network removed.

**No README command needed fixing** (it works as written and targets only `queryguard_demo`).
Note: PowerShell 5.1 printed `uv`'s stderr banner ("Using CPython …") wrapped as a
`NativeCommandError` because the capture used `2>&1` on a native exe; this is a capture
artifact, not a failure — `setup_demo` completed ("Done"). Per-step output is summarised
above (full run captured in this session's transcript).

## 0e. Reproduce commands — `python -m <module> --help`

| module | `-m … --help` works? | note |
|---|---|---|
| `scripts.load_bird` | **NO** | `ModuleNotFoundError: No module named '_dockerutil'` — uses a bare `from _dockerutil import …` that only resolves in the path form (`python scripts/load_bird.py`) |
| `scripts.split_bird` | **NO** | same bare `_dockerutil` import |
| `scripts.create_reader` | **NO** | same bare `_dockerutil` import |
| `eval.harness` | **YES** | prints argparse usage (exit 0) |
| `eval.analyze_phase6` | **YES** | prints argparse usage (exit 0) |

**Consequence for 1g:** the `-m` form works only for the eval modules, so the README uses
`-m` for `eval.harness` / `eval.analyze_phase6` and keeps the **path form**
(`python scripts/load_bird.py`, etc.) for the three setup scripts — because `-m` fails for
them and the scripts may not be edited. (CLAUDE.md's "`-m scripts.<name>` also works" is not
true for these three; it holds for scripts without a bare top-level import.) The path form is
safe for `scripts/` — the `eval/inspect.py` shadow that forces `-m` affects only `eval/`.
