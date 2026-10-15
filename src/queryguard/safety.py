"""SQL safety checking for QueryGuard.

This module turns a candidate SQL string into a :class:`CheckResult`: either a
single, re-rendered, LIMIT-capped read-only query that is safe to execute
(``safe_sql``), or a rejection with one actionable error. It is pure — no
database, no network, no file I/O — so it can be unit-tested in isolation and is
only the *second* safety layer (the read-only ``qg_reader`` role is the real
guarantee). The rules below are the contract; docs/safety_walkthrough.md explains
how each maps onto the installed sqlglot's node shapes.

check_sql rules, applied in order. The first failing rule returns ok=False,
safe_sql=None and one clear error message an AI can act on.

R1  Input must be non-empty after stripping whitespace, and at most MAX_SQL_CHARS
    characters.
R2  Parse with sqlglot.parse(sql, read=dialect). A parse failure is an error that
    includes the parser's message.
R3  Exactly one statement (empty statements from trailing semicolons are ignored).
R4  The root must be a read query: SELECT, UNION, INTERSECT or EXCEPT (a
    parenthesised query of these is allowed; WITH ... SELECT counts as SELECT).
    Everything else is rejected, including EXPLAIN and SHOW.
R5  No write or command node anywhere in the tree: INSERT, UPDATE, DELETE, MERGE,
    CREATE, DROP, ALTER, TRUNCATE, COPY, SET, GRANT, REVOKE, transaction
    statements, generic commands, SELECT ... INTO, and row-locking clauses
    (FOR UPDATE, FOR NO KEY UPDATE, FOR SHARE, FOR KEY SHARE).
R6  Every function call must be on an ALLOW-LIST of read-only functions
    (ALLOWED_FUNCTIONS, see queryguard.function_allowlist). A call passes only if
    its name — as this sqlglot version reports it, lower-cased with any schema
    prefix (e.g. pg_catalog.) and quoting already stripped — BOTH matches
    ^[a-z_][a-z0-9_]*$ AND is in ALLOWED_FUNCTIONS. Everything else is rejected, so
    R6 fails closed: dangerous built-ins, unknown/custom functions, and
    escaped/quoted names with unusual characters all fall through to the same
    rejection. Matching is on the exact reported name, never a substring (a column
    named sleep_hours is not a call and is fine). The former dangerous-function
    deny-list now lives only as a test fixture that must stay disjoint from the
    allow-list.
R7  LIMIT on the outermost query: none or LIMIT ALL -> set LIMIT max_rows;
    LIMIT n > max_rows -> set to max_rows; a non-literal LIMIT (expression,
    subquery, parameter) -> replace with max_rows; FETCH FIRST n ROWS ONLY is
    treated like LIMIT n; FETCH FIRST/NEXT n ROWS WITH TIES -> replaced with
    LIMIT min(n, max_rows), because the ties could otherwise return more than
    max_rows rows. OFFSET is kept. Every change adds a warning.
R8  Remove all comments from the checked tree, then render safe_sql with
    tree.sql(dialect=dialect). Re-parse safe_sql and re-apply R3, R4, R5 and R6 to
    the re-parsed tree; any failure there is reported as 'R8: ...' (the rendered
    SQL differs from what was checked). Only safe_sql may ever be executed, never
    the input string.
On success: ok=True, safe_sql set, errors=[], warnings list any LIMIT changes.

Every error message starts with its rule id and a colon, e.g. 'R5: write
statements are not allowed (found DELETE)'. Every warning starts with 'R7:'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from queryguard.function_allowlist import ALLOWED_FUNCTIONS

MAX_SQL_CHARS = 20_000

# R4: the only statement types that are a read query. Intersect/Except/Union all
# subclass SetOperation, so the pair below covers every allowed root.
_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (exp.Select, exp.SetOperation)

# R5: write / command / lock nodes. If any appears anywhere in the tree the query
# is not read-only. Top-level writes are already caught by R4 (their root is not a
# read query); this set matters for writes nested in CTEs or subqueries, for
# SELECT ... INTO, and for row-locking (FOR UPDATE/SHARE) clauses. exp.Command is
# sqlglot's catch-all for statements it does not model (VACUUM, GRANT, SET, ...).
_WRITE_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Into,
    exp.Lock,
)

# A friendly, AI-actionable name for each write node we might find nested.
_WRITE_DESCRIPTIONS: dict[type[exp.Expression], str] = {
    exp.Insert: "an INSERT",
    exp.Update: "an UPDATE",
    exp.Delete: "a DELETE",
    exp.Merge: "a MERGE",
    exp.Create: "a CREATE",
    exp.Drop: "a DROP",
    exp.Alter: "an ALTER",
    exp.Command: "a server command",
    exp.Into: "a SELECT ... INTO (it creates a table)",
    exp.Lock: "a row-locking clause (FOR UPDATE/SHARE)",
}

# R6: a real function name is a plain lower-case identifier. Anything else (e.g.
# the control characters sqlglot decodes from U&"\0070g_sleep") fails this shape
# check before we even look it up, closing parser-differential tricks without our
# having to decode escapes. The membership check against ALLOWED_FUNCTIONS (an
# allow-list of read-only functions) is what decides whether a call is permitted.
_FUNCTION_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class CheckResult:
    """The outcome of :func:`check_sql`.

    Attributes:
        ok: True iff the SQL passed every rule and ``safe_sql`` is executable.
        safe_sql: The re-rendered, LIMIT-capped SQL to execute, or None on failure.
        errors: Human/AI-readable reasons the SQL was rejected (empty on success).
        warnings: Notes about changes made (e.g. a LIMIT that was capped).
    """

    ok: bool
    safe_sql: str | None
    errors: list[str]
    warnings: list[str]


def _r1_input(sql: str) -> str | None:
    """R1: reject empty, whitespace-only, or over-long input."""
    if not sql.strip():
        return "R1: the SQL is empty. Provide a single SELECT query."
    # Measure the raw string: the cap guards the parser against huge inputs.
    if len(sql) > MAX_SQL_CHARS:
        return (
            f"R1: the SQL is {len(sql)} characters, over the {MAX_SQL_CHARS}-character "
            "limit. Shorten the query."
        )
    return None


def _r2_parse(sql: str, dialect: str) -> tuple[list[exp.Expression] | None, str | None]:
    """R2: parse, failing closed on any parser error or recursion blow-up."""
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    # Fail closed: a query we cannot parse is a query we cannot vouch for. A very
    # deeply nested statement overflows Python's stack inside sqlglot, so we treat
    # RecursionError as a parse failure too (see tests/test_safety_own.py).
    except (SqlglotError, RecursionError) as exc:
        return None, f"R2: could not parse the SQL ({exc}). Fix the syntax and retry."
    return parsed, None


def _r3_single_statement(
    statements: list[exp.Expression],
) -> tuple[exp.Expression | None, str | None]:
    """R3: require exactly one real statement; None entries are empty statements."""
    # sqlglot yields None for the empty statement left by a trailing ';' or a
    # comment-only input; those do not count.
    real = [statement for statement in statements if statement is not None]
    if len(real) != 1:
        return None, (
            f"R3: expected exactly one statement, found {len(real)}. Send a single SELECT query."
        )
    return real[0], None


def _unwrap(expression: exp.Expression) -> exp.Expression:
    """Peel off parenthesis wrappers to reach the real query underneath."""
    # "( SELECT ... )" parses to a Subquery/Paren whose .this is the inner query.
    while isinstance(expression, (exp.Subquery, exp.Paren)):
        expression = expression.this
    return expression


def _r4_read_root(tree: exp.Expression) -> str | None:
    """R4: the outermost statement must be SELECT / UNION / INTERSECT / EXCEPT."""
    root = _unwrap(tree)
    if isinstance(root, _ALLOWED_ROOTS):
        return None
    # VALUES and TABLE are read-only but not SELECTs, so they fail closed here. Give
    # the AI the SELECT rewrite rather than a bare rejection.
    if isinstance(root, exp.Values):
        return (
            "R4: a bare VALUES list is not allowed; wrap it in a SELECT, e.g. "
            "SELECT * FROM (VALUES (1), (2)) AS t(col)."
        )
    # Name the offending statement by its leading keyword, so the hint is concrete
    # (INSERT, EXPLAIN, VACUUM, ...), falling back to the node type if need be.
    rendered = root.sql(dialect="postgres").strip()
    keyword = rendered.split(None, 1)[0].upper() if rendered else type(root).__name__.upper()
    if keyword == "TABLE":
        # sqlglot misparses `TABLE t` as `t` aliased to the keyword TABLE; recover
        # the table name from the alias so we can suggest the SELECT form.
        name = root.alias if isinstance(root, exp.Alias) and root.alias else "the_table"
        return f"R4: use SELECT * FROM {name} instead of TABLE {name}."
    return (
        f"R4: only read queries are allowed; this is a {keyword} statement. Rewrite it as a SELECT."
    )


def _r5_no_writes(tree: exp.Expression) -> str | None:
    """R5: reject a write / command / lock node found anywhere in the tree."""
    # Walk the whole tree: a DELETE can hide in a CTE or a UNION branch, far from
    # the root, and SELECT ... INTO / FOR UPDATE sit on an otherwise-read query.
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            description = _WRITE_DESCRIPTIONS.get(
                type(node), f"a {type(node).__name__.upper()} operation"
            )
            return f"R5: {description} is not allowed; the query must only read data. Remove it."
    return None


def _function_name(node: exp.Func) -> str:
    """The lower-cased name of a function call node.

    An unknown function is an exp.Anonymous whose ``name`` is the raw identifier
    (schema prefix and quoting already stripped by the parser); a function sqlglot
    models is a subclass whose ``sql_name()`` is its canonical name.
    """
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    return (node.sql_name() or "").lower()


def _r6_function_allowlist(tree: exp.Expression) -> str | None:
    """R6: every function call must be a plain identifier on the allow-list."""
    for node in tree.walk():
        # Only function *calls* are checked; a column or string literal that merely
        # contains a function name is not a call and is left alone.
        if not isinstance(node, exp.Func):
            continue
        name = _function_name(node)
        # The shape check and the membership check share one rejection: a name that
        # is not a plain identifier can never be in the allow-list anyway, so both
        # dangerous built-ins and obfuscated names fail closed here.
        if not _FUNCTION_NAME_RE.match(name) or name not in ALLOWED_FUNCTIONS:
            return (
                f"R6: function {name} is not on the list of allowed read-only "
                "functions. Rewrite the query without it."
            )
    return None


def _is_int_literal(node: exp.Expression | None) -> bool:
    """True only for a bare integer literal such as the 5 in LIMIT 5."""
    return isinstance(node, exp.Literal) and node.is_int


def _literal_limit_value(limit_node: exp.Expression) -> int | None:
    """The integer value of a LIMIT/FETCH node, or None if it is not a literal."""
    # FETCH FIRST n ROWS ONLY keeps its count in the 'count' arg, not 'expression'.
    if isinstance(limit_node, exp.Fetch):
        count = limit_node.args.get("count")
        return int(count.name) if _is_int_literal(count) else None
    expression = limit_node.expression
    return int(expression.name) if _is_int_literal(expression) else None


def _is_with_ties(limit_node: exp.Expression) -> bool:
    """True for a FETCH ... WITH TIES, which can return more rows than its count."""
    # sqlglot stores the WITH TIES flag on the Fetch node's limit_options child.
    if not isinstance(limit_node, exp.Fetch):
        return False
    options = limit_node.args.get("limit_options")
    return bool(options is not None and options.args.get("with_ties"))


def _r7_limit(
    tree: exp.Expression, dialect: str, max_rows: int
) -> tuple[exp.Expression, list[str]]:
    """R7: cap the outermost LIMIT at max_rows, warning on any change."""
    warnings: list[str] = []
    query = _unwrap(tree)  # the outermost query; a subquery's LIMIT does not count
    current = query.args.get("limit")

    if current is None:
        # No LIMIT at all (postgres also drops LIMIT ALL to this) -> impose the cap.
        query.limit(max_rows, copy=False)
        warnings.append(f"R7: no row limit was set; added LIMIT {max_rows}.")
        return tree, warnings

    if _is_with_ties(current):
        # WITH TIES returns every row tied with the n-th, so the FETCH count is not a
        # true row cap. Replace it with a plain LIMIT min(n, max_rows); limit() drops
        # the FETCH (and its WITH TIES) entirely.
        count = _literal_limit_value(current)
        limit_value = min(count, max_rows) if count is not None else max_rows
        query.limit(limit_value, copy=False)
        warnings.append(f"R7: WITH TIES removed; at most {limit_value} rows returned.")
        return tree, warnings

    value = _literal_limit_value(current)
    if value is not None and value <= max_rows:
        return tree, warnings  # a literal LIMIT within the cap is left untouched

    # Over the cap, or a non-literal LIMIT we will not trust -> force max_rows.
    # limit(copy=False) mutates this query in place (and replaces any FETCH).
    reason = (
        f"LIMIT {value} exceeds the cap" if value is not None else "the LIMIT is not a plain number"
    )
    query.limit(max_rows, copy=False)
    warnings.append(f"R7: {reason}; set LIMIT to {max_rows}.")
    return tree, warnings


def _verify_rendered(safe_sql: str, dialect: str) -> str | None:
    """Re-check the rendered SQL: re-parse it and re-apply R3, R4, R5, R6.

    We only ever execute ``safe_sql``, so we must trust it, not the input. In
    principle rendering can change meaning (a comment that becomes code, quoting,
    a dialect quirk), so the string that leaves the checker must itself pass the
    same structural rules. Any failure here means the rendered SQL no longer
    matches what R3-R6 approved, so it is reported under R8.
    """
    try:
        statements = sqlglot.parse(safe_sql, read=dialect)
    # Fail closed, exactly as R2 does: unrenderable-then-unparseable is not safe.
    except (SqlglotError, RecursionError) as exc:
        return f"R8: the rendered SQL did not re-parse ({exc}). Do not run it."
    tree, error = _r3_single_statement(statements)
    if error is None:
        # tree is the single re-parsed statement; re-run the structural rules.
        for rule in (_r4_read_root, _r5_no_writes, _r6_function_allowlist):
            error = rule(tree)  # type: ignore[arg-type]
            if error:
                break
    if error:
        return f"R8: the rendered SQL differs from what was checked ({error}). Do not run it."
    return None


def _r8_render(tree: exp.Expression, dialect: str) -> tuple[str | None, str | None]:
    """R8: strip comments, render safe_sql, then re-verify what will actually run."""
    # Drop every comment first: sqlglot would otherwise re-emit them, and a comment
    # is attacker-controlled noise that must never ride along in the query we run.
    for node in tree.walk():
        node.comments = None
    safe_sql = tree.sql(dialect=dialect)
    error = _verify_rendered(safe_sql, dialect)
    if error:
        return None, error
    return safe_sql, None


def check_sql(sql: str, dialect: str = "postgres", max_rows: int = 100) -> CheckResult:
    """Check that ``sql`` is a single safe read-only query; see the module spec.

    Args:
        sql: The candidate SQL text (never executed directly).
        dialect: The sqlglot dialect to parse and re-render with.
        max_rows: The hard cap applied to the outermost LIMIT.

    Returns:
        A :class:`CheckResult`. On success ``ok`` is True and ``safe_sql`` is the
        only string that may be executed.
    """
    error = _r1_input(sql)
    if error:
        return CheckResult(False, None, [error], [])

    statements, error = _r2_parse(sql, dialect)
    if error:
        return CheckResult(False, None, [error], [])

    tree, error = _r3_single_statement(statements)  # type: ignore[arg-type]
    if error:
        return CheckResult(False, None, [error], [])

    # R4-R6 inspect the tree; the first to object rejects the query.
    for rule in (_r4_read_root, _r5_no_writes, _r6_function_allowlist):
        error = rule(tree)
        if error:
            return CheckResult(False, None, [error], [])

    tree, warnings = _r7_limit(tree, dialect, max_rows)

    safe_sql, error = _r8_render(tree, dialect)
    if error:
        return CheckResult(False, None, [error], [])

    return CheckResult(True, safe_sql, [], warnings)
