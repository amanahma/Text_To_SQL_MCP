# Phase 6B runbook

Exact commands for the 6B run, in order, all in **`-m` form** (never the path form —
`eval/inspect.py` shadows the stdlib `inspect` module when `eval/` is `sys.path[0]`).
Every `-m` command here was checked with `--help` only (no API calls). All fingerprint
files and attack/run outputs go under `results/` (not `logs/` or `eval/`). Commands are
PowerShell-friendly: one per line, no `&&`.

## Prerequisites (one-time)
- Database up: `docker compose up -d`.
- `.env` has `EVAL_PROVIDER=groq`, `EVAL_MODEL=openai/gpt-oss-120b`, `GROQ_API_KEY=…`,
  and (for the plain attack run) `PLAIN_PASSWORD=…`.
- Roles present: `uv run python scripts/create_reader.py` and, for the plain setting,
  `uv run python scripts/create_plain.py` (grants qg_plain EXECUTE on the REVOKE'd functions;
  prints qg_plain=true / qg_reader=false and exits non-zero if not).
- Groq free tier, several accounts. When a run stops at the daily limit, the owner puts a key
  from a different account into .env, then the same command is re-run. Finished question ids are
  skipped; an interrupted question restarts from scratch. Each switch is logged in
  results/RUN_LOG.md as 'account A/B/C...' (never the key).

## Commands (in order)

0. **Readiness gate — must be all PASS before starting.**
   ```
   uv run python -m eval.phase6_ready
   ```

1. **Fingerprint before**
   ```
   uv run python -m scripts.db_fingerprint --out results/fingerprint_6b_before.json
   git add results/fingerprint_6b_before.json
   git commit -m "6B: fingerprint before"
   ```

2. **Baseline, 150 questions** (resumable; re-run the same line to resume after a provider error)
   ```
   uv run python -m eval.harness --condition baseline --sample eval/sample_150.json --concurrency 1 --resume results/baseline_6b.jsonl
   git add results/baseline_6b.jsonl
   git commit -m "6B: baseline 150 run"
   ```

3. **Main, 150 questions** (resumable)
   ```
   uv run python -m eval.harness --condition main --sample eval/sample_150.json --concurrency 1 --resume results/main_6b.jsonl
   git add results/main_6b.jsonl
   git commit -m "6B: main 150 run"
   ```

4. **Attacks, reader setting — one command per category** (model-driven; outcomes + layers
   only, no timing). `eval/run_attacks.py` has **no resume and writes `--out` only once, at
   the very end**: a Groq daily-limit stop mid-run raises `ProviderError` before that write,
   so the whole invocation is lost. Split by category so a stop loses only the in-progress
   category; finished categories keep their own `--out`. Run each line; on a daily-limit
   stop switch the key (log account A/B/C in RUN_LOG) and re-run that one line.
   ```
   uv run python -m eval.run_attacks --role reader --categories hint_injection --out results/attacks_reader_hint_injection_6b.json
   uv run python -m eval.run_attacks --role reader --categories parser_trick --out results/attacks_reader_parser_trick_6b.json
   uv run python -m eval.run_attacks --role reader --categories pii --out results/attacks_reader_pii_6b.json
   uv run python -m eval.run_attacks --role reader --categories resource --out results/attacks_reader_resource_6b.json
   uv run python -m eval.run_attacks --role reader --categories side_effect --out results/attacks_reader_side_effect_6b.json
   uv run python -m eval.run_attacks --role reader --categories system_access --out results/attacks_reader_system_access_6b.json
   uv run python -m eval.run_attacks --role reader --categories write_ddl --out results/attacks_reader_write_ddl_6b.json
   git add results/attacks_reader_*_6b.json
   git commit -m "6B: attacks reader (30, one file per category)"
   ```

5. **Attacks, plain setting — one command per category** (needs qg_plain + PLAIN_PASSWORD;
   only `side_effect`/`system_access` are meaningful under `--role plain`). Same no-resume
   caveat as step 4 — each category is its own `--out`.
   ```
   uv run python -m eval.run_attacks --role plain --categories side_effect --out results/attacks_plain_side_effect_6b.json
   uv run python -m eval.run_attacks --role plain --categories system_access --out results/attacks_plain_system_access_6b.json
   git add results/attacks_plain_*_6b.json
   git commit -m "6B: attacks plain (11, one file per category)"
   ```

6. **Fingerprint after** — then diff against step 1; row counts MUST be identical.
   ```
   uv run python -m scripts.db_fingerprint --out results/fingerprint_6b_after.json
   git add results/fingerprint_6b_after.json
   git commit -m "6B: fingerprint after"
   ```

7. **Analyze** (no API calls; reads the run files + tags + prices)
   ```
   uv run python -m eval.analyze_phase6 --baseline results/baseline_6b.jsonl --main results/main_6b.jsonl --sample eval/sample_150.json --tags eval/tags.json --prices eval/prices.json --out results/phase6_report_6b.md
   git add results/phase6_report_6b.md
   git commit -m "6B: analysis report"
   ```

8. **Drop the plain control role** (after the attacks are done)
   ```
   uv run python -m scripts.drop_plain
   ```

## Timing evidence (separate; no model, no API)
Per-attack elapsed ms comes from the scripted dry run, not the model-driven runs above
(see PREREGISTRATION.md). It runs 3 times and reports median + min–max ms per row:
```
uv run python -m eval.dryrun_attacks
```
It writes `results/attack_dryrun.md` and asserts the row-count fingerprint is unchanged.

## Notes
- `--resume` writes the run file at the given path (here under `results/`), creating it on the
  first run and appending on later runs; done ids are skipped. A verified provider/DB outage is
  the only allowed re-run per PREREGISTRATION.md.
- `eval/run_attacks.py` (steps 4–5) is model-driven and needs the API key; it reports
  model_refused | blocked | contained | harm and the layer. Do not use it for timing.
- Commit the run files as you go (steps 2–7) so a provider interruption never loses progress.
