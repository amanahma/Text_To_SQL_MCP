# Phase 7 Part 7 — release checks

Date 2026-10-15. No git/gh commands. No Groq API calls. `.env` and
`results/errors_key.csv` were never opened or printed (sizes via `stat` only).

## 1. attacks_6b_bundle.zip

- `ls -la attacks_6b_bundle.zip` → `No such file or directory` (exit 2).
- `python glob('*.zip') + glob('**/*.zip')` (excluding `.venv`) → `[]`.

**The zip does not exist** (it was present in Phase 1 at the repo root, 25,935 bytes, but is
gone now — removed between sessions). Nothing to delete. **No other zips** exist in the repo.

## 2. MIT LICENSE

- `git config user.name` (read only) → `Aman Ahmad`.
- Wrote `LICENSE`: MIT, `Copyright (c) 2026 Aman Ahmad`.
- README Licence section set to: "Code: MIT (see LICENSE). BIRD Mini-Dev data and any
  BIRD-derived content follow BIRD's own licence (see Reproduce)."

## 3. BIRD Mini-Dev licence + BIRD-derived files

**Licence found: CC BY-SA 4.0.** Source: the BIRD Mini-Dev GitHub repo README badge.

- Repo URL: <https://github.com/bird-bench/mini_dev>
- Licence URL: <https://creativecommons.org/licenses/by-sa/4.0/deed.en>
- Verbatim README badge line (split, it exceeds the fetch quote limit):
  - `[![License](https://img.shields.io/badge/License-CC%20BY%20SA%204.0-orange.svg)`
  - `https://creativecommons.org/licenses/by-sa/4.0/deed.en`
- The README has no separate code-vs-data licence text; its only other use statement is
  "Please cite the repo if you think our work is helpful to you."

README `Reproduce` "Licence: verify" was **replaced** with the CC BY-SA 4.0 licence + URL.

### Repo files containing BIRD-derived content (report only; sizes in bytes)

Content = questions / evidence / gold SQL / result rows embedded in the file.

| file | size | contains |
|---|---|---|
| `data/BIRD_dev.sql` | 1,001,882,533 | raw BIRD PostgreSQL dump (gitignored) |
| `data/mini_dev_postgresql.json` | 283,525 | the 500 BIRD questions/evidence/gold SQL (gitignored) |
| `results/main_6b.jsonl` | 2,700,317 | transcripts: questions, SQL, result rows |
| `results/baseline_6b.jsonl` | 691,935 | transcripts: questions, SQL, result rows |
| `results/errors_blind.csv` | 114,530 | questions, evidence, gold+pred SQL, result rows |
| `results/errors_key.csv` | 3,665 | same as errors_blind (size via stat; **not opened**) |
| `results/attack_cells_6b.csv` | 23,455 | attack SQL + cell results (BIRD tables/values) |
| `redteam_results.json` | 22,587 | red-team SQL against BIRD tables |
| `results/attack_dryrun.md` | 10,268 | gold/pred SQL snippets, PII sample values |
| `results/attack_summary_6b.md` | 9,302 | gold/pred SQL snippets, PII sample values |
| `results/main_6b.errors.jsonl` | 6,503 | error records (question ids / SQL) |
| `results/baseline_6b.errors.jsonl` | 4,642 | error records (question ids / SQL) |
| `results/phase6_report_6b.md` | 24,815 | sample gold/pred SQL in the write-up |
| `results/phase6_exploratory.md` | 4,186 | per-question ids + non-answer detail |
| `results/main_tool_text.txt` | 4,042 | BIRD schema/tool text sample |
| `results/probe_discovery_pii.txt` | 1,480 | BIRD PII column sample values |
| `eval/gold_issues.md` | 1,395 | commentary on specific BIRD gold queries |
| `results/transcripts/attacks_reader_resource_6b_transcripts.json` | 15,679 | tool calls: SQL + results |
| `results/transcripts/attacks_reader_pii_6b_diag_transcripts.json` | 15,032 | tool calls: SQL + results |
| `results/transcripts/attacks_reader_system_access_6b_transcripts.json` | 10,438 | tool calls: SQL + results |
| `results/transcripts/attacks_plain_system_access_6b_transcripts.json` | 10,419 | tool calls: SQL + results |
| `results/transcripts/attacks_reader_hint_injection_6b_transcripts.json` | 7,407 | tool calls: SQL + results |
| `results/transcripts/attacks_reader_side_effect_6b_transcripts.json` | 6,826 | tool calls: SQL + results |
| `results/transcripts/attacks_plain_side_effect_6b_transcripts.json` | 6,077 | tool calls: SQL + results |
| `results/transcripts/attacks_reader_parser_trick_6b_transcripts.json` | 2,660 | tool calls: SQL + results |
| `results/transcripts/attacks_reader_write_ddl_6b_transcripts.json` | 2,106 | tool calls: SQL + results |

Not BIRD content — **question ids / selection metadata only** (listed for completeness, not
counted above): `eval/sample_150.json` (2,486), `eval/sample_50.json` (565),
`eval/dev_10.json` (197), `eval/tags.json` (1,251). `results/errors_labels.csv` and
`results/spotcheck_20.csv` reference rows by `blind_id` (spotcheck also carries question text
copied from errors_blind).

## 4. Transcript review

Wrote `results/transcript_review_3cells.md` ("AI-extracted summary for the owner to read")
covering hint_inject_pii (main/reader), se_wal_emit_nontxn (baseline/plain),
r_cross_join_players (main/reader): per-call tool/SQL/result, final text, official label +
relabel, and a match line. Read from `results/transcripts/*` (allowed in Part 7).

## 5. MCP Inspector CLI (demo stack up)

`node v25.8.0`, `npx 11.11.0`. First `docker compose up` failed with a transient
`npipe://…dockerDesktopLinuxEngine … daemon` error (Docker Desktop was mid-restart);
`docker info` then returned ServerVersion 29.8.1 and the stack came up on retry with
`--build`. Ran `scripts.setup_demo`, then MCP Inspector in CLI mode (launching the server
via `docker exec -i queryguard-demo-server-1 python -m queryguard.server`). **It worked** —
all three calls returned the expected structured result:

- `--tool-arg "sql=DROP TABLE books"` → `isError:true`, `blocked: R4: only read queries are allowed; this is a DROP statement. Rewrite it as a SELECT.`
- `--tool-arg "sql=SELECT pg_ls_dir('.')"` → `isError:true`, `blocked: R6: function pg_ls_dir is not on the list of allowed read-only functions. Rewrite the query without it.`
- `--tool-arg "sql=SELECT email FROM customers ORDER BY customer_id LIMIT 5"` → `isError:false`, 5 rows, every `email` cell `***`.

`docs/DEMO_SCRIPT.md` parts (c) and (d) were updated with these exact tested commands and
the "not tested" note was removed. The demo stack was torn down (`down -v`): volume and
network removed.

## 6. .gitignore

Already contained `.env`, `.venv/`, `data/`, `results/transcripts/`, `__pycache__/`.
**Added the missing `*.zip` line** (file edit only; no git commands). Full required set now
present.

## 7. Commands (summary) and skips

| step | command | result |
|---|---|---|
| 1 | `ls -la attacks_6b_bundle.zip`; `glob **/*.zip` | not found; no zips |
| 2 | `git config user.name` | `Aman Ahmad`; LICENSE written |
| 3 | WebFetch `github.com/bird-bench/mini_dev` (+ raw README) | CC BY-SA 4.0 badge |
| 4 | read `results/transcripts/*` | review written |
| 5 | `docker compose up --build` / `setup_demo` / `npx … inspector --cli …` ×3 / `down -v` | Inspector works; blocks R4/R6; email masked |
| 6 | edit `.gitignore` | `*.zip` added |

**Skipped / not done:**
- Step 1 deletion: nothing to delete — the zip was already absent.
- No file under `src/`, `eval/`, `tests/`, `PREREGISTRATION.md`, `create_reader.sql`, or any
  existing results file (other than the new `results/release_checks.md`,
  `results/transcript_review_3cells.md` and the README/.gitignore/LICENSE/DEMO_SCRIPT edits)
  was changed. `.env` and `results/errors_key.csv` were never opened.
