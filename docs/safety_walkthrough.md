# safety.py walkthrough

How `check_sql` turns a candidate SQL string into either a single, re-rendered,
LIMIT-capped read-only query (`safe_sql`) or a rejection with one actionable
error. This is the *second* safety layer — the read-only `qg_reader` role is the
real guarantee (see "What check_sql does NOT stop" below).

`check_sql` is **pure**: no database, no network, no file I/O. It runs rules
R1–R8 in order and returns on the first failure. Every error message starts with
its rule id (`"R4: ..."`); every warning starts with `"R7:"`. Node shapes below
were verified against the installed sqlglot (30.21.0) in a REPL — do not trust
memory, re-check if you upgrade.

---

## R1 — input shape

**Blocks:** empty string, whitespace-only input, and anything longer than
`MAX_SQL_CHARS` (20,000) characters.

**Code path:** `_r1_input`. `sql.strip()` falsy → empty; `len(sql) > MAX_SQL_CHARS`
→ too long. The length is measured on the *raw* string, before parsing, so a huge
input never reaches the parser.

**Edge cases:** the test builds the over-length case as `SELECT '<20001 a's>'` — a
valid-looking query that is rejected purely on length.

**Limitation:** 20,000 chars is a blunt guard, not a complexity measure; a short
query can still be expensive (that is R7 + the executor's job).

---

## R2 — parse (fail closed)

**Blocks:** anything sqlglot cannot parse — gibberish, unbalanced quotes/parens,
and pathologically deep nesting.

**sqlglot:** `sqlglot.parse("SELECT 'abc FROM t", read="postgres")` raises
`sqlglot.errors.SqlglotError`. 3,000 nested parentheses raise `RecursionError`
*inside* sqlglot (Python stack overflow), which is **not** a `SqlglotError`.

**Code path:** `_r2_parse` catches **only** `(SqlglotError, RecursionError)` and
returns an `"R2:"` error that includes the parser's message. No broad `except`.
"Fail closed": a query we cannot parse is one we cannot vouch for, so we reject it
rather than guess.

**Edge cases:** `tests/test_safety_own.py::test_deep_nesting_is_blocked_not_crashed`
pins the RecursionError path.

**Limitation:** the error text embeds sqlglot's message, which can be terse.

---

## R3 — exactly one statement

**Blocks:** stacked statements (`SELECT 1; DROP TABLE x`), a second statement
hidden after a comment or after a string literal containing `;`, and comment-only
input (zero statements).

**sqlglot:**
- `parse("SELECT 1; DELETE FROM t")` → `[Select, Delete]` (two).
- `parse("-- just a comment")` → `[None]` (zero real statements).
- `parse("SELECT 1;")` → `[Select]` (a single trailing `;` adds no entry).

**Code path:** `_r3_single_statement` drops `None` entries (empty statements) and
requires exactly one; otherwise `"R3:"`.

**Edge cases:** `None` entries are how sqlglot represents the empty statement from
a trailing `;`, so `SELECT 1;` passes but `SELECT 1; SELECT 2` does not.

---

## R4 — the root must be a read query

**Blocks:** any statement whose root is not `SELECT / UNION / INTERSECT / EXCEPT`
— INSERT/UPDATE/DELETE/MERGE/DDL, transaction control, `EXPLAIN`, `SHOW`,
`VACUUM`, etc.

**sqlglot:** `Intersect`/`Except`/`Union` all subclass `exp.SetOperation`, so the
allow-list is `(exp.Select, exp.SetOperation)`. `WITH ... SELECT` parses to a
`Select` (the CTEs hang off its `with` arg). A parenthesised query
`(SELECT ...)` parses to `exp.Subquery` (or `exp.Paren`) wrapping the real query.
Unsupported statements (`VACUUM`, `SHOW`, `EXPLAIN`) parse to `exp.Command`.

**Code path:** `_r4_read_root` first `_unwrap`s `Subquery`/`Paren` to reach the
real query, then checks `isinstance(root, _ALLOWED_ROOTS)`. The error names the
offending statement by its leading keyword (e.g. "this is a VACUUM statement").

**`VALUES` and `TABLE` (fail closed, with a rewrite hint):** both are read-only but
are *not* `SELECT`s, so they stay rejected — but the error points the AI at the
SELECT form rather than just naming the keyword:
- `VALUES (1), (2)` (root `exp.Values`) → "R4: a bare VALUES list is not allowed;
  wrap it in a SELECT, e.g. `SELECT * FROM (VALUES (1), (2)) AS t(col)`."
- `TABLE schools` → this sqlglot *misparses* `TABLE t` as `t` aliased to the
  keyword `TABLE` (an `exp.Alias` over `Column("TABLE")`), so R4 recovers the table
  name from the alias and returns "R4: use `SELECT * FROM schools` instead of
  `TABLE schools`." (`TABLE schema.name` does not parse at all → R2.) See
  "Known limitations".

**Edge cases:** top-level writes are caught *here* (root is `Insert`, etc.), not
R5 — which is why the attack YAML labels `INSERT ...` as R4. We keep the original
tree for rendering and only unwrap a copy of the reference for the type check.

---

## R5 — no write / command / lock node anywhere

**Blocks:** writes nested where R4 cannot see them — data-modifying CTEs
(including `MATERIALIZED`), writes in a UNION branch — plus `SELECT ... INTO`
(and `INTO TEMP`) and row-locking clauses (`FOR UPDATE [OF t]`, `FOR SHARE`, ...).

**sqlglot:**
- `WITH x AS (DELETE FROM t RETURNING id) SELECT * FROM x` → an `exp.Delete` node
  lives deep in the tree; `tree.walk()` finds it.
- `SELECT * INTO t FROM s` → the `Select` carries an `into` arg of type `exp.Into`.
- `SELECT ... FOR UPDATE OF schools` → an `exp.Lock` node.

**Code path:** `_r5_no_writes` walks the **whole** tree (not just the root) and
rejects the first node in `_WRITE_NODES`
`(Insert, Update, Delete, Merge, Create, Drop, Alter, Command, Into, Lock)`, with
a friendly description per type.

**Edge cases:** by the time R5 runs, R4 has guaranteed the root is a read query;
R5's job is the *nested* cases. `exp.Command` is sqlglot's catch-all for statements
it does not model, included for defence in depth.

**Limitation:** the set is keyed on node classes verified in this sqlglot version;
a future version that models a new write statement as a new class would need it
added here (and none of the 500 gold queries contain any of these nodes, so the
set is exercised only by attacks).

---

## R6 — every function must be on the allow-list

**Phase 2C flipped R6 from a deny-list to an allow-list.** The old list of
dangerous names could never be complete — the red-team found live functions it
missed (`pg_logical_emit_message`, `txid_current`, `lo_get`, `dblink_send_query`,
`pg_ls_waldir`, ...). Now a function call is permitted **only** if its name is on
`ALLOWED_FUNCTIONS` (see `queryguard.function_allowlist`), so anything unknown —
dangerous built-in or custom UDF — fails closed.

**Blocks:** every function call whose reported name is not a plain lower-case
identifier **or** is not in `ALLOWED_FUNCTIONS`. One error for both:
"R6: function `<name>` is not on the list of allowed read-only functions. Rewrite
the query without it."

**sqlglot:** an unmodelled function is `exp.Anonymous` whose `.name` is the raw
identifier with schema prefix and quoting already stripped
(`SELECT pg_catalog.pg_sleep(10)` and `SELECT "pg_sleep"(10)` both yield
`Anonymous(this=pg_sleep)`). A modelled built-in is a `Func` subclass whose
`sql_name()` is its **canonical** name — and sqlglot *renames* many: `date_trunc`
→ `timestamp_trunc`, `string_agg` → `group_concat`, `random` → `rand`, `now` →
`current_timestamp`, `char_length` → `length`, `to_char` → `time_to_str`,
`generate_series` → `exploding_generate_series`, `unnest` → `explode`. The
allow-list therefore stores the **reported** forms (verified in a REPL), plus the
SQL spellings for robustness. It also includes the core-SQL operators/keywords
this sqlglot models as `Func` (`AND`, `OR`, `CASE`, `EXISTS`, quantifiers,
`COLLATE`, the array/json containment and regex operators) — these are syntax, not
callable functions, and never carry side effects.

**Code path:** `_r6_function_allowlist` walks the tree; for every `exp.Func` (this
includes table functions in `FROM`, e.g. `SELECT * FROM pg_sleep(5)`) it computes
the lower-cased name via `_function_name` and rejects unless it both matches
`^[a-z_][a-z0-9_]*$` **and** is in `ALLOWED_FUNCTIONS`.

**Edge cases:**
- A column named `pg_sleep` or `sleep_hours`, or the string `'pg_sleep(5)'`, is
  **not** a function call, so none are `exp.Func` and none are blocked.
- `U&"\0070g_sleep"(5)` — see "Parser differentials" below; the shape check rejects
  it before the lookup even runs.
- `DATE(x)` is reported as the function `date`, which is **not** on the allow-list;
  it is the cast `x::date` in disguise (see "Known limitations" / red-team report).

**The old deny-list** lives on only as a test fixture (`OLD_DANGEROUS_FUNCTIONS` in
`tests/test_safety_redteam_functions.py`), which asserts it is disjoint from the
allow-list and that every name on it is still blocked.

**Limitation:** the allow-list is built from PostgreSQL 16's documented read-only
function categories, not from the gold queries, so a legitimate-but-unlisted
function is a false block by design — reported for a decision, never silently
added (that keeps the gold test an independent check).

---

## R7 — cap the outermost LIMIT

**Blocks nothing; rewrites.** On the *outermost* query: no LIMIT or `LIMIT ALL`
→ add `LIMIT max_rows`; `LIMIT n > max_rows` → set to `max_rows`; a non-literal
LIMIT (expression / subquery / parameter) → replace with `max_rows`;
`FETCH FIRST n ROWS ONLY` is treated like `LIMIT n`; **`FETCH FIRST/NEXT n ROWS
WITH TIES` → replaced with `LIMIT min(n, max_rows)`.** `OFFSET` is kept. Every
change appends an `"R7:"` warning.

**Why WITH TIES is special (red-team finding):** `WITH TIES` returns *every* row
tied with the n-th on the `ORDER BY` key, so the FETCH count is not a true row cap.
`… FETCH FIRST 1 ROW WITH TIES` on `schools` returned **761 rows** against
`max_rows=100` while R7 left it untouched and gave no warning. R7 now strips
`WITH TIES` by replacing the whole clause with a plain `LIMIT`, so the cap holds.

**sqlglot:** the limit lives in the query's `limit` arg as `exp.Limit`
(`.expression` is the value) or `exp.Fetch` (`.args["count"]` is the value). The
`WITH TIES` flag hangs off the Fetch node's `limit_options` child
(`limit_options.args["with_ties"]`). Postgres drops `LIMIT ALL` at parse time, so it
arrives as *no* limit. `query.limit(n, copy=False)` mutates the query in place and
replaces any existing LIMIT **or** FETCH (so the FETCH and its `WITH TIES` are gone).

**Code path:** `_r7_limit` `_unwrap`s to the outermost query (a LIMIT inside a
subquery does not count), reads the current limit, and:
- none → add `max_rows`, warn;
- a `WITH TIES` FETCH (`_is_with_ties`) → `query.limit(min(n, max_rows))`, warn
  "WITH TIES removed; at most `<limit>` rows returned";
- a literal `n <= max_rows` → leave untouched, no warn;
- anything else (over cap, or non-literal via `_literal_limit_value` returning
  `None`) → `query.limit(max_rows, copy=False)`, warn.

**Edge cases:** `LIMIT 0` is a literal within the cap → kept. `LIMIT 2 + 3` and
`LIMIT (SELECT 5)` are non-literal → forced to `max_rows`. For a set operation the
LIMIT belongs to the whole UNION, which is the outermost query. `FETCH FIRST n ROWS
ONLY` (no ties) is still treated like `LIMIT n`.

**Limitation:** a non-literal LIMIT is *replaced*, not evaluated — a legitimate
`LIMIT 2 + 3` becomes `LIMIT max_rows`. That is intentional: we never trust an
expression we did not compute.

---

## R8 — render, strip comments, round-trip

**Blocks:** (defensively) any checked tree that does not re-render to exactly one
statement.

**sqlglot:** `tree.sql(dialect="postgres")` renders the (possibly rewritten) tree.
sqlglot **re-emits input comments** by default, so `_r8_render` first clears
`node.comments` on every node — a comment is attacker-controlled noise that must
not ride along. `safe_sql` is then re-parsed and must yield exactly one statement.

**Code path:** `_r8_render` strips comments, renders, re-parses, checks count.

**Edge cases:** the comment-stripping is why `SELECT School -- note\nFROM ...`
produces `safe_sql` with no `--` or `/* */`.

**Limitation:** the round-trip failure branch is defensive — no input that passes
R1–R7 is known to trip it — so it is the one path not covered by tests (coverage
98%, the two uncovered lines are this guard).

---

## Parser differentials — own_a03 and why an allow-list beats decoding

`own_a03` is `SELECT U&"\0070g_sleep"(5)`. In **real PostgreSQL**, `U&"..."` is a
Unicode-escaped identifier: `\0070` decodes to U+0070 = `p`, so this calls
`pg_sleep(5)` — a real sleep.

**sqlglot does not agree.** It does not treat `U&"..."` as the Unicode-escape
syntax at all: it parses `U` as a column, `&` as bitwise-and, and `"\0070g_sleep"`
as a quoted identifier, decoding the escape *differently* — to a control character
plus `0g_sleep`. If R6 did not exist, `tree.sql()` would render
`SELECT U & "\x070G_SLEEP"(5)` — neither the original text nor `pg_sleep`.

This is a **parser differential**: the checker (sqlglot) and the executor
(Postgres) disagree about what the bytes mean. A deny-list that compares decoded
text is only as good as the decoder matching Postgres exactly — and here it does
not. The **allow-list** (`^[a-z_][a-z0-9_]*$`) sidesteps the whole problem: we
never need to decode correctly, because any function name that is not already a
plain identifier is rejected outright. Combined with the rule that **only
`safe_sql` is ever executed** (never the input string), the escaped form can never
reach Postgres as `pg_sleep`.

---

## Known limitations (all fail closed)

Three legitimate read-only inputs are rejected on purpose, because allowing them
safely is not worth the parser risk. Each fails **closed** (rejected, never run)
with a hint for the AI:

- **`VALUES (1), (2)`** — a bare VALUES list is read-only but not a `SELECT`, so R4
  rejects it with a hint to wrap it: `SELECT * FROM (VALUES (1), (2)) AS t(col)`.
- **`TABLE schools`** — shorthand for `SELECT * FROM schools`; R4 rejects it with
  "use `SELECT * FROM schools` instead of `TABLE schools`". (`TABLE schema.name`
  does not even parse in this sqlglot → R2.)
- **`SELECT 1 LIMIT ALL`** (a `FROM`-less projection) — valid PostgreSQL, but this
  sqlglot cannot parse `LIMIT ALL` without a `FROM`, so R2 rejects it. Add a `FROM`
  or drop the `LIMIT ALL`.
- **`DATE(x)`** — reported by sqlglot as the function `date`, which is not on the
  allow-list (it is the cast `x::date`). Two gold queries use it; it is reported for
  a decision rather than silently allowed. Rewrite as `x::date` / `CAST(x AS date)`.

---

## Red-team results (Phase 2B → fixed in Phase 2C)

The Phase 2B red-team attacked `check_sql` and executed everything it allowed
against the live database as `qg_reader`. What it found, and how each was closed:

| Finding | Severity | What actually happened | Fix |
|---|---|---|---|
| `pg_logical_emit_message(...)` | CRITICAL | Wrote a message to the **WAL** (advanced `pg_current_wal_lsn` by 104 bytes) through the read-only role — the read-only transaction did not stop it. | R6 allow-list rejects it; `create_reader.sql` revokes EXECUTE from PUBLIC. |
| `txid_current()` / `pg_current_xact_id()` | CRITICAL | **Assigned a real XID** (`pg_current_xact_id_if_assigned()` went NULL → 2016) — a global state change in a read-only txn. | R6 allow-list rejects; EXECUTE revoked. |
| `setseed(0.5)` | HIGH | Executed and mutated session RNG state. | R6 allow-list rejects; EXECUTE revoked. |
| `FETCH FIRST n ROWS WITH TIES` | MEDIUM | Returned **761 rows** vs `max_rows=100`; R7 left it untouched, no warning. | R7 converts `WITH TIES` to `LIMIT min(n, max_rows)`. |
| `lo_get`/`lo_put`, `dblink_send_query`/`_connect_u`/`_get_result`, `pg_ls_logdir`/`_waldir`/`_tmpdir`, `pg_current_logfile`, `pg_switch_wal`, `pg_create_restore_point`, `pg_stat_reset`, `pg_promote`, `pg_replication_origin_create`, `current_setting`, `currval`/`lastval` | LOW (stopped by another layer: missing extension / privilege / read-only txn) | `check_sql` allowed them, but the `qg_reader` role or a missing `dblink` extension refused them. They were deny-list completeness gaps. | R6 allow-list now rejects all of them (the whole class, not a fixed list). |
| `VALUES`, `TABLE`, `SELECT 1 LIMIT ALL` | FALSE BLOCK | Legitimate read-only queries `check_sql` rejected. | Owner decision: keep rejecting (fail closed), but R4/R2 now give rewrite hints. See "Known limitations". |

The CRITICAL/HIGH/MEDIUM findings were fixed in **two layers**: R6's switch to an
allow-list (so the whole class of unknown/side-effect functions fails closed, not
just names someone remembered) and the database revoking EXECUTE on the four
functions that actually executed. The LOW findings needed no new DB work — the
read-only role already refused them — but R6 now blocks them too.

---

## What check_sql does NOT stop (other layers must)

`check_sql` is structural: one statement, read-only root, no write nodes, no
dangerous functions, a capped LIMIT. It says nothing about *cost*. Read-only but
expensive queries pass it by design and are the job of later layers — the
read-only `qg_reader` role, the read-only transaction + server-side
`statement_timeout` (5s), the EXPLAIN cost warning, and the row cap. Examples from
`tests/safety_cases/resource_attacks.yaml`, all of which pass `check_sql`:

- `res_huge_generate_series` — `SELECT count(*) FROM generate_series(1, 1000000000)`:
  a billion rows, stopped by `statement_timeout`.
- `res_cross_join_transactions` — a self cross join of a large table: quadratic
  work, stopped by the timeout / EXPLAIN cost warning.
- `res_unbounded_recursive` — a `WITH RECURSIVE` with no stop condition: runs
  forever, stopped by the timeout.
- `res_order_by_random` — `ORDER BY random()` over two million rows: a huge sort,
  stopped by the timeout.
- `res_catastrophic_regex` — `body ~ '(a+)+b'`: catastrophic backtracking,
  stopped by the timeout.

The row cap (R7's `LIMIT`) bounds the *result* size, not the work done to produce
it; that is why the timeout is the real defence against these.

---

## 10 hard questions about this file

1. **Why catch `RecursionError` in R2 when it is not a `SqlglotError`?**
   Very deep nesting overflows Python's stack *inside* sqlglot's recursive parser,
   raising `RecursionError`. Without catching it, `check_sql` would crash instead
   of rejecting the query. We fail closed and report it as `"R2:"`.

2. **A top-level `INSERT` — is it blocked by R4 or R5?** R4. Its root is
   `exp.Insert`, not a read query, and rules run in order so R4 returns first.
   R5 exists for writes R4 cannot see (CTEs, UNION branches) and for `INTO`/locks.

3. **Why walk the whole tree in R5 and R6 instead of checking the root?** A
   data-modifying `DELETE` can hide in a CTE or a UNION branch, and a dangerous
   function can sit in `WHERE`, `ORDER BY`, a `CASE`, or a `FROM` table function.
   Only a full `tree.walk()` sees them.

4. **Why an allow-list on function names, not just a deny-list?** To defeat parser
   differentials (own_a03). A deny-list on decoded text trusts sqlglot's decoder
   to match Postgres; the allow-list rejects any non-identifier name regardless of
   how it decodes.

5. **How is `LIMIT ALL` handled, given it is not in the code?** The Postgres
   dialect drops `LIMIT ALL` at parse time, so the query arrives with *no* limit
   and hits the "none → add `max_rows`" branch. (A bare `SELECT 1 LIMIT ALL` with
   no FROM fails to parse and is an R2 reject — a sqlglot quirk.)

6. **Why `query.limit(n, copy=False)` rather than building a new tree?** sqlglot
   builder methods copy by default; `copy=False` mutates the existing query in
   place so the surrounding tree (e.g. a `Subquery` wrapper) keeps pointing at it,
   and a single call replaces an existing `LIMIT` *or* `FETCH`.

7. **Why does R8 strip comments, and why there?** sqlglot re-emits input comments
   by default, so a comment would survive into `safe_sql`. Stripping happens right
   before rendering, the only place the final string is produced, so nothing
   attacker-controlled rides along.

8. **What stops `SELECT count(*) FROM generate_series(1,1e9)`?** Not `check_sql` —
   it is a valid read query. The read-only role, the 5s `statement_timeout`, and
   the EXPLAIN cost warning do. `check_sql` bounds result *rows*, not *work*.

9. **Why is a non-literal `LIMIT 2 + 3` replaced instead of evaluated?** We never
   execute or trust an expression we did not compute; evaluating arithmetic (or a
   subquery, or a parameter) in the checker invites surprises. Replacing with
   `max_rows` is safe and still caps the result.

10. **Could a query pass every rule and still be unsafe?** Yes — against *cost*
    (resource attacks) and against *data exposure*. `check_sql` is layer 2; the
    read-only role, timeout, EXPLAIN cost check, row cap, PII masking and audit log
    are the other layers. `check_sql` never claims to be sufficient on its own.
