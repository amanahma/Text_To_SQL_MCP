# README numbers (A3)

Every number the README will use, with its value and the file + field/line it is read
from. **No new statistics were computed here** — each value is copied from a file already
on disk. Where a requested value is in no file, it is marked **MISSING**.

## Primary accuracy

| number | value | source file | field / line |
|---|---|---|---|
| baseline correct count | 81/150 (54.0%) | `results/phase6_report_6b.md` | §1 "baseline: **81/150 = 54.0%**" |
| main correct count | 75/150 (50.0%) | `results/phase6_report_6b.md` | §1 "main: **75/150 = 50.0%**" |
| point estimate (main − baseline) | −4.0 points | `results/phase6_report_6b.md` | §1 "point estimate (main − baseline): **−4.0 points**" |

## 2×2 paired agreement

| cell | value | source file | field / line |
|---|---|---|---|
| both right | 63 | `results/phase6_report_6b.md` | §1 2×2 table, baseline-right × main-right |
| baseline-only right (b) | 18 | `results/phase6_report_6b.md` | §1 2×2 table / "discordant pairs ... b = 18" |
| main-only right (c) | 12 | `results/phase6_report_6b.md` | §1 2×2 table / "c = 12" |
| both wrong | 57 | `results/phase6_report_6b.md` | §1 2×2 table, baseline-wrong × main-wrong |

## Inference

| number | value | source file | field / line |
|---|---|---|---|
| McNemar exact two-sided p | 0.3616 | `results/phase6_report_6b.md` | §1 "exact two-sided McNemar p: **0.3616**" |
| paired bootstrap 95% CI (main − baseline) | [−11.3, +3.3] points | `results/phase6_report_6b.md` | §1 "paired bootstrap 95% CI (10,000 resamples, seed 1234): **[−11.3, +3.3]**" |
| Wilson 95% — baseline | 46.0–61.8% | `results/phase6_report_6b.md` | §1 "baseline: 81/150 ... (Wilson 95%: 46.0–61.8%)" |
| Wilson 95% — main | 42.1–57.9% | `results/phase6_report_6b.md` | §1 "main: 75/150 ... (Wilson 95%: 42.1–57.9%)" |
| non-inferiority margin | 10 percentage points | `eval/PREREGISTRATION.md` | "Non-inferiority margin: **10 percentage points**" (also `results/phase6_report_6b.md` §1) |
| preregistered verdict | inconclusive at n=150; no directional claim | `results/phase6_report_6b.md` | §1 "Preregistered outcome: **inconclusive at n = 150**" |

## Cost and latency (exploratory)

| number | value | source file | field / line |
|---|---|---|---|
| dollar cost — baseline | $0.1581 | `results/phase6_report_6b.md` | §2 "dollar cost: baseline **$0.1581**" |
| dollar cost — main | $0.4273 | `results/phase6_report_6b.md` | §2 "main **$0.4273**" |
| mean latency — baseline | 32.6 s/question | `results/phase6_report_6b.md` | §2 "latency seconds per question: baseline mean 32.6" |
| mean latency — main | 134.1 s/question | `results/phase6_report_6b.md` | §2 "main mean 134.1" |

## Tool-call cap (MAX_TOOL_CALLS = 10) hits

| number | value | source file | field / line |
|---|---|---|---|
| tool-cap hits — baseline | 4 | `results/phase6_report_6b.md` | §2 "questions that hit `MAX_TOOL_CALLS` (10): baseline **4**" |
| tool-cap hits — main | 14 | `results/phase6_report_6b.md` | §2 "main **14**" |
| main tool calls blocked by check_sql / cost gate | none (0) | `results/phase6_report_6b.md` | §2 "main tool calls blocked by check_sql or the cost gate ... **none**" |

## Controlled attack dry run (scripted, no model)

| number | value | source file | field / line |
|---|---|---|---|
| dry-run rows | 82 (41 attack×setting cells × 2 conditions) | `results/phase6_report_6b.md` §4a; `results/attack_dryrun.md` | "**82 rows**"; table body |
| mismatches vs expected | 0 | `results/phase6_report_6b.md` §4a; `results/attack_dryrun.md` | "**0 mismatches**"; every row "match" |
| number of runs | 3 | `results/phase6_report_6b.md` §4a; `results/attack_dryrun.md` | "run **3 times**"; header "3 runs" |
| rows that changed outcome across runs | 0 | `results/attack_dryrun.md` | "No row changed outcome across the 3 runs." |
| dry-run fingerprint before == after | True | `results/attack_dryrun.md`; `results/phase6_report_6b.md` §4a | "Fingerprint before == after ... **True**" |

## Attack counts per setting and condition — relabelled categories

Source: `results/phase6_report_6b.md` §4b "Relabelled view" table (post-hoc relabel,
one label per cell; official `model_refused` replaced by its decomposition). `results/attack_summary_6b.md` §(a) holds the per-cell official-vs-relabelled breakdown.

| setting | condition | n | harm | harm_flagged_discovery_only | blocked | contained | validated_only | wrote_sql_not_executed | text_refusal | ignored injection, answered benign (other) |
|---|---|---|---|---|---|---|---|---|---|---|
| reader | baseline | 30 | 5 | 0 | 6 | 0 | 0 | 12 | 6 | 1 |
| reader | main | 30 | 0 | 1 | 5 | 0 | 7 | 8 | 8 | 1 |
| plain | baseline | 11 | 2 | 0 | 1 | 0 | 0 | 6 | 2 | 0 |
| plain | main | 11 | 0 | 0 | 3 | 0 | 1 | 5 | 2 | 0 |

Official classifier counts (harm | blocked | contained | model_refused), same source §4b second table:

| setting | condition | n | harm | blocked | contained | model_refused |
|---|---|---|---|---|---|---|
| reader | baseline | 30 | 5 | 6 | 0 | 19 |
| reader | main | 30 | 1 | 5 | 0 | 24 |
| plain | baseline | 11 | 2 | 1 | 0 | 8 |
| plain | main | 11 | 0 | 3 | 0 | 8 |

Note: the first reader/pii run (`results/attacks_reader_pii_6b.json`) has no transcript
and is reported separately with official labels only (`results/phase6_report_6b.md` §4b).

## Repository / integrity

| number | value | source file | field / line |
|---|---|---|---|
| test count (pytest collected) | 550 | measured 2026-10-14 by `uv run pytest --collect-only -q` | "550 tests collected"; recorded in `results/phase7_checks.txt` |
| row-count fingerprint before == after (6B) | equal (identical `row_counts`) | `results/phase6_report_6b.md` §5; `results/fingerprint_6b_before.json` / `_after.json` | "Row-count fingerprint before == after: **equal**" (files differ only in `taken_at`) |
| prompt_version in every run record | a16401d6 | `results/phase6_report_6b.md` §5 | "`prompt_version` value in every record of both files: **`a16401d6`**" |
| total rows across 11 databases (fingerprint) | 3,898,114 (11 databases, 75 tables) | `results/RUN_LOG.md`; `results/fingerprint_6b_before.json` | RUN_LOG 2026-10-09 11:46 "DONE Step 1" |

## Values requested but not found in any file

- **MISSING: "fingerprint equality" as a boolean field inside the fingerprint JSON.**
  The equality is asserted in prose in `results/phase6_report_6b.md` §5 ("equal") and the
  dry-run equality (`True`) in `results/attack_dryrun.md`; the fingerprint JSON files
  themselves (`fingerprint_6b_before.json` / `_after.json`) hold only `row_counts` and
  `taken_at`, not a precomputed equality flag. No separate statistic was computed here.
