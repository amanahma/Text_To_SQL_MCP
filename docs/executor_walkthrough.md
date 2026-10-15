# executor.py walkthrough

How `run_checked` takes a database name and a candidate SQL string and returns an
`ExecResult` that is always safe to hand back to an AI. This is safety **layers
3–5** (read-only transaction + statement timeout, the EXPLAIN cost gate, and the
row cap / masking / audit). Layer 2 is `check_sql` (see `safety_walkthrough.md`);
layer 1 is the read-only `qg_reader` role. `run_checked` only ever executes the
`safe_sql` that `check_sql` returns — never the caller's text.

`run_checked` returns an `ExecResult` for every SQL or database problem (it does
not raise for those). The single deliberate exception is an unknown database
name, which `require_known_database` raises before anything else runs.

Numbers below (costs, timings) were measured against the installed PostgreSQL 16
image and asyncpg 0.31.0. Re-check them if you rebuild the data or upgrade.

---

## The pipeline, step by step

`run_checked(database, sql, user_id="local", max_rows=100)`:

1. **`require_known_database(database)`** — `database` must be one of `BIRD_DBS`.
   An unknown name raises `UnknownDatabaseError` *before* the SQL is parsed or the
   database is touched; the MCP tool turns that into a tool error.

2. **`check_sql(sql, max_rows=max_rows + 1)`** — layer 2. On failure we build a
   `"blocked"` `ExecResult` from `check.errors`, write an audit line, and return.
   **The database is never touched for blocked SQL.** On success we hold
   `check.safe_sql` (the only string we will execute) and `check.warnings`.

3. **Per-user concurrency gate** — `async with _semaphore_for(user_id)` limits any
   one user to `MAX_CONCURRENT_PER_USER` (3) in-flight queries, so a single caller
   cannot monopolise the pools. The semaphore is remembered with the event loop
   that created it and rebuilt if the loop changes (asyncio primitives bind to a
   loop; this keeps tests, each with their own loop, correct).

4. **`_execute`** opens a pooled connection, a `transaction(readonly=True)`, and
   `SET LOCAL statement_timeout = '5s'`, then runs two steps in that one
   transaction:
   - **`_cost_gate`** — EXPLAINs `safe_sql` and refuses with `"cost"` if the
     planner's top-node Total Cost exceeds `EXPLAIN_COST_LIMIT`. Always runs.
   - **`_fetch_rows`** — executes `safe_sql` with a server-side cursor, fetching
     `max_rows + 1` rows.
   Any database/connection exception is mapped to an actionable `ExecResult` by
   `_classify_error`.

5. **Shape the result** — `_convert_rows` makes every cell JSON-safe, truncates
   long text, and masks PII columns; `_rewrite_warnings` turns R7's LIMIT notes
   into one AI-facing "results are capped at N rows".

6. **Audit** — `_audit` appends one JSON line per call (off the event loop via
   `asyncio.to_thread`). Written for *every* call, including blocked and
   unavailable. Never contains a secret or connection string.

The three real steps — **check → cost gate → execute** — are separate and
unconditional. There is no argument, on `run_checked`, on `_execute`, or on the
tool, that can switch the cost gate off (hard rule 3). The timeout is proven end
to end by a query that genuinely clears the cost gate (see below), not by a seam.

---

## Why `max_rows + 1`

The caller asks for at most `max_rows` (100) rows and also needs to know whether
there were *more*. So:

- `check_sql` is handed `max_rows + 1`, so R7 caps the outermost `LIMIT` at 101,
  not 100.
- `_fetch_rows` fetches 101 rows. If 101 come back, there was at least one beyond
  the cap: we set `truncated = True` and return the first 100.

Fetching exactly `max_rows` could never distinguish "exactly 100 rows" from "100
of many". The `+1` is the cheapest possible lookahead. We never tell the AI about
the 101: `_rewrite_warnings` reports only "results are capped at 100 rows", so the
off-by-one is an implementation detail, not something a model could trip over.

---

## Why *our* code builds the EXPLAIN

The cost gate runs `EXPLAIN (FORMAT JSON) <safe_sql>` where `<safe_sql>` is the
exact string `check_sql` approved and the exact string `_fetch_rows` will execute
— concatenated by our code, never the caller's text. If we let the caller supply
the EXPLAIN target (or EXPLAINed the raw input), a query could be cost-checked in
one form and executed in another. Building it ourselves guarantees the estimate
describes the statement we actually run.

`EXPLAIN` (without `ANALYZE`) only *plans* the query; it does not execute it, so
estimating the cost of a billion-row cartesian product is cheap and safe.

asyncpg hands the `EXPLAIN (FORMAT JSON)` column back as a JSON **string**, so
`_estimate_cost` `json.loads` it and reads `plan[0]["Plan"]["Total Cost"]`.

---

## How `EXPLAIN_COST_LIMIT` was calibrated — and why from gold only

`scripts/calibrate_explain.py` ran `check_sql(..., max_rows=101)` then
`EXPLAIN (FORMAT JSON) <safe_sql>` for all **500 BIRD gold queries**, each as
`qg_reader` on its *own* database in a read-only transaction, and recorded the
top-node Total Cost. Results (see `logs/explain_calibration.csv`):

| statistic | gold Total Cost |
| --------- | --------------- |
| max       | **45,515.75** (the `financial` database) |
| p99       | 26,010 |
| p95       | 20,217 |
| median    | 486 |

`EXPLAIN_COST_LIMIT = 460_000` = **10 × the gold maximum (455,157.5), rounded up**
to a round number. Every real gold query clears it with an order of magnitude of
headroom, so the gate never refuses legitimate work; the runaway cartesian
products in `resource_attacks.yaml` (costs 1.8e6–2.7e12) are refused before they
execute.

**Why from gold only:** the limit answers "how expensive does a *legitimate*
query ever get?" Gold is our ground truth for that. If we tuned the limit by
looking at which attacks it happens to block, we would be fitting the threshold to
the attack set we happen to have — a number that looks good on today's attacks and
tells us nothing about tomorrow's. The attack refusal count is *reported after*
the limit is fixed, never used to move it. (In practice the choice is robust: any
limit in roughly `[1.0e5, 1.8e6]` blocks the same six attacks.)

---

## Soft estimate vs. hard limit — the recursion example

The cost gate is a **soft** guard built on the planner's *estimate*, and the
planner can be very wrong. Recursive CTEs are the classic case: Postgres applies a
fixed, tiny guess for how many times the recursive term will run.

Case `res_unbounded_recursion_count`:

```sql
WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r)
SELECT count(*) FROM r
```

- **EXPLAIN cost ≈ 5.55** — far below 460,000, so it *clears the cost gate*.
- There is no stop condition, and `COUNT(*)` forces the whole (infinite) CTE to be
  consumed, so the row cap cannot help either — `COUNT` returns one row no matter
  how many it counted.
- The query therefore runs until the **hard 5s `statement_timeout`** cancels it:
  `run_checked` returns `error_kind="timeout"` with measured `elapsed_ms ≈ 5033`.

This is why the timeout is the real backstop. The cost gate skims off the queries
the planner *can* see are enormous; the statement timeout catches everything else,
including the ones the planner under-costs. `test_statement_timeout_maps_to_timeout`
exercises exactly this query through the real `run_checked` path.

---

## Why the row cap also contains resource abuse

The `LIMIT max_rows + 1` that R7 imposes is not only about response size — it is a
third containment mechanism. A query like `SELECT ... ORDER BY random()` or
`ORDER BY md5(...)` *looks* like it must sort the whole table, but with a small
`LIMIT` Postgres switches to a **top-N heapsort**: it keeps only the best 101 rows
seen so far, so the work is O(rows) with tiny memory instead of a full sort. On the
BIRD data the `ORDER BY random()`/`md5` "attacks" return in well under a second
because of this, never reaching the timeout.

So a resource attack can end in any of three contained states, and
`tests/test_resource_attacks.py` asserts exactly that union:

- **`cost`** — the cost gate refused it (the extreme cartesian products);
- **`timeout`** — the 5s timer cancelled it (the unbounded-recursion count);
- **row-capped `ok`** — the LIMIT turned it cheap (the top-N sorts) and it returned
  ≤ 100 rows.

Which mechanism catches a *borderline* case (a heavy sort, a nasty regex) depends
on the data and the machine, so the test asserts containment, not a fixed outcome
per case.

---

## PII masking — rules and limits

`_is_pii_column(name)` masks a whole column (every cell becomes `"***"`) when any
**whole-word token** of the column name is in `PII_TOKENS`
(`email, phone, mobile, aadhaar, pan, ssn, password, token, secret`). The name is
lower-cased and split on non-alphanumerics, and matched token-by-token:

- `customer_email` → `["customer", "email"]` → masked.
- `user_ssn`, `pan_card` → masked.
- `company` → `["company"]` → **not** masked (it is not the token `pan`).
- `pantry` → `["pantry"]` → **not** masked.

Whole-word matching is what stops the substring false positives ("compan**y**",
"**pan**try") that a naive `"pan" in name` would mask.

**Limits — this is a safety net, not a classifier:**

- It keys on column *names*, not values. A PII value sitting in a blandly-named
  column (`notes`, `col1`, a `SELECT email AS x`) is **not** masked. The list is a
  convenience for well-named schemas, not a guarantee.
- It is English/token-based. A column named in another language, or spelled
  outside the token list, is missed; `PII_TOKENS` is deliberately a short,
  reviewable, configurable set rather than a fuzzy matcher.
- Masking replaces the entire cell, so a partially-sensitive field is fully
  hidden rather than redacted in part.

---

## How errors map to hints

`_classify_error` turns a database exception into a single-line, AI-actionable
`error`, tagged with an `error_kind`:

| condition | `error_kind` | message the AI sees |
| --------- | ------------ | ------------------- |
| `check_sql` rejected the SQL | `blocked` | the first `check_sql` error (starts with its rule id) |
| planner estimate over the limit | `cost` | the estimate, the limit, and "add filters, aggregate, or narrow the query" |
| `QueryCanceledError` (statement timeout) | `timeout` | "query exceeded 5 seconds; add filters, aggregate, or narrow the query." |
| `UndefinedColumnError` / `UndefinedTableError` | `sql_error` | the first line + up to 3 closest real names (difflib) + "Check names with list_tables." |
| `InsufficientPrivilegeError` | `sql_error` | "this function or table is not allowed." |
| any other `PostgresError` | `sql_error` | the first line of the message |
| connection refused/reset/closed (`OSError`, `InterfaceError`, or a connection `PostgresError`) | `unavailable` | "database unreachable: &lt;first line&gt;" |

Two design choices: we only ever surface the **first line** of a database message
(never a multi-line server dump or a stack trace), and the undefined-name hint
draws its suggestions from that database's own cached table/column names, so the
model gets a concrete next step ("did you mean `city`?") instead of a dead end.

---

## Why "database unreachable" matters for the eval

The eval harness compares QueryGuard against a baseline by running the model's SQL
and scoring it. If the database goes down mid-run, every query would "fail" — and
without care that would be scored as the *model* getting the answers wrong,
silently poisoning the accuracy numbers. So the executor is careful to label a
connectivity failure `unavailable` with a message that starts **"database
unreachable:"**, and the harness lists `"database unreachable"` among its
infrastructure-error signatures. When that phrase appears in a tool result, the
harness *stops the whole run* (and prints a resume command) instead of recording a
model failure. The specific, stable prefix is the contract between the two layers;
changing it would break the harness's ability to tell an outage from a wrong
answer.

---

## Eight hard questions

1. **Why fetch `max_rows + 1` instead of running `COUNT(*)` to learn whether there
   were more rows?**
   A separate `COUNT(*)` is a second query over the same (possibly expensive) plan
   and can disagree with the fetch under concurrent writes. One extra row is free,
   consistent with the rows actually returned, and needs no second round trip.

2. **The cost gate passed a query, but it still timed out. Is the gate broken?**
   No. The gate is a *soft* estimate from the planner, which under-costs some
   queries (recursive CTEs especially). It is a first filter, not a guarantee; the
   hard guarantee is the 5s `statement_timeout`. The two are complementary.

3. **Why EXPLAIN our own `safe_sql` rather than the caller's SQL?**
   So the cost estimate describes the exact statement we will execute. EXPLAINing
   the raw input could check one query and run another (comments, quoting, dialect
   re-rendering by R8). We only trust, and only run, `safe_sql`.

4. **Why is `EXPLAIN_COST_LIMIT` derived from gold and not from the attacks?**
   The limit encodes "how expensive can a legitimate query be"; gold is the ground
   truth for that. Fitting it to the attack set would overfit to today's attacks
   and generalise poorly. The attack refusal count is a *report*, computed after
   the limit is frozen.

5. **Could an attacker avoid both the cost gate and the timeout?**
   To dodge the gate they need a low planner estimate; to dodge the timeout they
   need to finish within 5s; and the row cap (top-N) defuses the "sort everything"
   shape. A query that is cheap to plan, finishes in 5s, and returns ≤ 100 rows is,
   by definition, not a resource attack — it is just a normal query.

6. **Why is the cost gate in the same transaction as the execution?**
   So the estimate and the run see the same snapshot and the same `SET LOCAL`
   timeout, and so there is a single acquire/release. Splitting them would add a
   race (the plan could change between connections) for no benefit.

7. **A column holds emails but is named `contact`. Is it masked? What does that
   tell you about the masking?**
   No — masking keys on the column *name*'s tokens, and `contact` has none in
   `PII_TOKENS`. It tells you the masking is a convenience for well-named schemas,
   not a data classifier; it must not be relied on as the sole control over
   sensitive data.

8. **Why write an audit line for a `blocked` query, which never touched the
   database?**
   The audit log records *decisions*, not just executions. "We refused this SQL
   before touching the database" is exactly the kind of event an operator wants to
   see. The line stores `safe_sql: null` for a blocked call (there is no safe SQL),
   so a reader can tell refusals from runs at a glance.
