# Phase 6 analysis report

- sample: 10 questions  (primary N = 150  -- SMALL/dry run)
- model: openai/gpt-oss-120b  reasoning_effort: medium  prompt_version: a16401d6
- non-inferiority margin: 10.0 points

## Primary: execution accuracy
- baseline: 7/10 = 70.0% (Wilson 95%: 39.7-89.2%)
- main:     6/10 = 60.0% (Wilson 95%: 31.3-83.2%)

## 2x2 agreement (paired)
| | main right | main wrong |
|---|---|---|
| **baseline right** | 6 | 1 |
| **baseline wrong** | 0 | 3 |

- discordant pairs: only-baseline-right b=1, only-main-right c=0

## Paired difference (main - baseline)
- point estimate: -10.0 points
- paired bootstrap 95% CI: [-30.0, +0.0] points (10000 resamples, seed 1234)
- exact two-sided McNemar p: 1.0000 (b=1, c=0)

## Non-inferiority verdict (preregistered)
- 'main not worse than baseline' (CI lower > -10.0): **NO** (lower=-30.0)
- 'main better than baseline' (whole CI > 0): **NO**
- -> inconclusive at this n; no directional claim.

## Secondary metrics (reported; no standalone claims)

### Accuracy by difficulty
| difficulty | n | baseline | main |
|---|---|---|---|
| simple | 3 | 2/3 (67%) | 2/3 (67%) |
| moderate | 5 | 4/5 (80%) | 4/5 (80%) |
| challenging | 2 | 1/2 (50%) | 0/2 (0%) |

### Accuracy by db_id
| db_id | n | baseline | main |
|---|---|---|---|
| california_schools | 1 | 1/1 | 1/1 |
| card_games | 1 | 1/1 | 1/1 |
| european_football_2 | 1 | 0/1 | 0/1 |
| financial | 1 | 1/1 | 1/1 |
| student_club | 1 | 1/1 | 1/1 |
| superhero | 2 | 2/2 | 1/2 |
| thrombosis_prediction | 1 | 1/1 | 1/1 |
| toxicology | 2 | 0/2 | 0/2 |

### Accuracy excluding each tag (one tag at a time)
| excluded tag | removed | n | baseline | main |
|---|---|---|---|---|
| pii_masked | 0 | 10 | 7/10 (70.0%) | 6/10 (60.0%) |
| gold_issue | 1 | 9 | 7/9 (77.8%) | 6/9 (66.7%) |
| empty_gold | 0 | 10 | 7/10 (70.0%) | 6/10 (60.0%) |

### no_sql count
- baseline: 0   main: 1

### Answered-only accuracy (no_sql excluded from denominator)
- baseline: 7/10 (70.0%)   main: 6/9 (66.7%)

### Questions that hit MAX_TOOL_CALLS (10)
- baseline: 0 []   main: 0 []

### Tokens per question
- baseline:
    - prompt_tokens: mean=3409.5 median=2228.5 p90=4436.5
    - completion_tokens: mean=592.4 median=358.5 p90=1247.7
    - reasoning_tokens: mean=416.1 median=209.0 p90=936.7
    - cached_tokens: mean=204.8 median=0.0 p90=204.8
- main:
    - prompt_tokens: mean=13116.6 median=11899.0 p90=17815.0
    - completion_tokens: mean=1127.7 median=996.5 p90=1808.7
    - reasoning_tokens: mean=705.9 median=607.0 p90=1279.0
    - cached_tokens: mean=1152.0 median=512.0 p90=2380.8

### Tool calls per question
- baseline: mean=1.7 median=1.0 p90=2.3
- main:     mean=6.3 median=6.0 p90=8.1

### Main per-tool call counts (total over the sample)
- list_databases: 10
- list_tables: 8
- describe_table: 22
- find_columns: 6
- validate_sql: 9
- execute_query: 8

### Latency (seconds per question)
- baseline: mean=21.4 median=13.1 p90=30.4
- main:     mean=81.5 median=78.9 p90=134.9

### Dollar cost
- baseline: $0.0087  (prompt=34095, completion=5924; rates $0.15/$0.6 per 1M)
- main: $0.0264  (prompt=131166, completion=11277; rates $0.15/$0.6 per 1M)

## Main tool calls blocked by check_sql or the cost gate
- none

## Correct answers where gold AND prediction are both empty
(gold empty = empty_gold tag OR gold.row_count==0; pred empty = pred.row_count==0)
- none
