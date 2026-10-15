# Phase 6 evaluation — preregistration

Written before any Phase 6 run. It fixes the metric, settings, comparison, margin
and re-run rule in advance so the result cannot be reshaped after seeing numbers.
Nothing here is tuned on the test set. The two conditions (baseline vs main) stay
separate everywhere: **baseline** carries the full schema in its one tool's
description; **main** does not and must discover the schema with its tools.

## Question set (frozen)
- Test set: `eval/sample_150.json` — 150 questions, seed 6150, sha256 recorded in
  the file and re-checked by `eval/check_sample.py`. Superset of `sample_50`,
  disjoint from `dev_10`. Difficulty: 45 simple / 75 moderate / 30 challenging.
  All 11 db_ids present.
- `dev_10` was used for prompt/tool development and is **not** in the test set. No
  further tuning on `sample_150` of any kind.

## Primary metric
- **Execution accuracy on the 150 questions**, judged by `eval/scoring.py`
  (unchanged; owner-written). A `no_sql` result counts as **wrong**.
- **One run per condition** (baseline, main). No best-of, no averaging over runs.

## Settings (read from code on 2026-10-07; the run must match these)
- Provider / model: Groq, `openai/gpt-oss-120b` (fixed; never compare across models).
- reasoning_effort: `medium` (`EVAL_REASONING_EFFORT` default in `harness._reasoning_effort_for`).
- temperature: `0` (hard-coded in the Groq loop).
- max output tokens per turn (`MAX_TOKENS`): `2048`.
- Prompt: version `a16401d6` (unchanged). Only prompt difference between conditions
  is main's extra line `Always pass database="{db_id}" to tools that take a database argument.`
- `MAX_TOOL_CALLS`: `10`.
- Row cap (`DEFAULT_MAX_ROWS`): `100`; server-side `statement_timeout`: `5s`.
- Concurrency: `1` (required by the Groq free-tier limits).
- Final-SQL extractor: `harness.extract_last_sql` — the last ```` ```sql ```` fenced
  block; else the last plain ```` ``` ```` block that begins with SELECT/WITH; else
  `None` (→ `no_sql`). Same extractor for both conditions.

## Primary comparison and claims
- Paired per-question correctness on the same 150 ids.
- **Point estimate:** main accuracy − baseline accuracy (in percentage points).
- **Interval:** 95% CI on the paired difference from a **paired bootstrap** —
  resample the 150 questions with replacement, 10,000 resamples, seed **1234**,
  2.5th/97.5th percentiles of (main − baseline) accuracy.
- **Test:** exact two-sided **McNemar** on the discordant pairs
  (b = only-baseline-right, c = only-main-right), p = binomial two-sided tail with
  `math.comb` (no scipy).
- **Non-inferiority margin:** **10 percentage points** (DEFAULT — the owner may
  change this value before Phase 6A ends; the run uses whatever is recorded here).
- **Claim "main is not worse than baseline"** only if the CI lower bound on
  (main − baseline) is **above −margin** (i.e. > −10 points).
- **Claim "main is better"** only if the entire CI is **above 0**.
- If neither holds, report "inconclusive at n=150"; make no directional claim.

## Secondary metrics (reported, no standalone claims)
Accuracy by difficulty and by db_id; accuracy with each tag's questions excluded
(`pii_masked`, `gold_issue`, `empty_gold`) one at a time; `no_sql` count per
condition; answered-only accuracy (exclude `no_sql` from the denominator); count of
questions that hit `MAX_TOOL_CALLS`; token usage (prompt, completion, reasoning,
cached) mean/median/p90; tool calls per question; per-tool counts for main
(list_databases, list_tables, describe_table, find_columns, validate_sql,
execute_query); latency; dollar cost (only if `eval/prices.json` is filled);
main tool calls blocked by check_sql or the cost gate.

## Attack-suite evidence (separate from the accuracy claim)
- Per-attack **timing evidence** (elapsed ms per attack/condition) comes from the
  **scripted dry run** (`eval/dryrun_attacks.py`, no model, no API; wall time measured
  around each exec tool call), recorded in `results/attack_dryrun.md`.
- The **model-driven** attack runs (`eval/run_attacks.py`) report **outcomes and layers
  only** (model_refused | blocked | contained | harm, and the blocking layer) — not timing.
- The attack suite is a safety/containment check and is not part of the primary accuracy
  metric or its claims.

## Re-run rule
- A question is re-run **only** for a provider or database error (an outage):
  the harness raises `InfrastructureError`/`ProviderError` and writes no record;
  resume with `--resume` so done ids are skipped. Verified-outage resumes do not
  invalidate the run.
- A **wrong** or **no_sql** result is **never** re-run.
- Runs use the Groq free tier across several accounts. A question interrupted by a rate limit
  restarts from scratch on the next key. This counts as a provider stop, not a re-run of a
  wrong answer.
- **No** change to the prompt, tools, extractor or scoring after the first run.
- If anything else breaks (a bug, an accidental setting change, a scoring change),
  the run is declared **invalid** and restarted from zero with a written reason;
  partial results are not reported as the result.
- A 400 tool_use_failed error (the model called a tool when not allowed, or
  produced a malformed tool call) is a model failure, not a provider outage. The
  first attempt counts: the question is scored no_sql in that condition. If the
  harness re-runs it on resume, the later record is ignored. Same rule for both
  conditions.
- Harness fix on 2026-10-10, before the freeze tag: a tool_use_failed 400 on the
  final turn now follows the designed path (up to 3 final-turn attempts, then
  final_turn_failed, scored wrong), the same in both conditions. Before the fix,
  the error escaped to the errors file. This happened only for baseline q32, which
  stays scored no_sql under the first-attempt rule. The fix stops failed questions
  from being re-run on every resume.
- Second harness fix, before the freeze tag: a per-minute 429 whose text contained
  'today' was wrongly treated as a daily limit and stopped the run. Now only
  explicit daily limits (TPD/RPD) stop it. This changes when a run stops, never a
  score. The 16 baseline records written before both fixes are unaffected.

## Exploratory (not part of the claim)
- Large-schema test: designed **after** the main result is known, reported
  separately, and never folded into the primary accuracy or its claims.

## Provenance (fill after commit)
- git commit hash: Frozen by git tag phase6-eval. Get the hash with: git rev-parse phase6-eval. This file must not change after the tag.
- git tag: `phase6-eval` (annotated)
- sample_150 sha256: `395f92680b75741a57458faf8a1cbfdf9dc0ad0bce89b294ffc115c8deb345db`
- Non-inferiority margin in force at run time: `10` points (update here if changed before 6A ends).
