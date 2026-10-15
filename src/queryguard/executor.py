"""Run a checked SQL query against one BIRD database, safely.

This is safety layers 3-5 (the read-only transaction + statement timeout, the
EXPLAIN cost gate, and the row cap / masking / audit). Layer 2 — :func:`check_sql`
— is called here first and is the only thing that decides what SQL is safe; this
module never executes anything but the ``safe_sql`` it returns.

The single entry point is :func:`run_checked`. It always returns an
:class:`ExecResult` (it never raises for a SQL or database problem), so the MCP
tool layer can turn a failure straight into an actionable hint for the model. The
one deliberate exception is an unknown database name, which
:func:`require_known_database` raises before anything else happens.

Pipeline (see the phase spec):
  1. require_known_database(database)
  2. check_sql(sql, max_rows + 1) — if it fails, the database is never touched.
  3. a pooled connection, a read-only transaction, SET LOCAL statement_timeout
  4. EXPLAIN (FORMAT JSON) of *our* safe_sql; refuse if the planner's top-node
     Total Cost exceeds EXPLAIN_COST_LIMIT (never execute an over-budget query)
  5. execute only safe_sql with a server-side cursor, fetching max_rows + 1 to
     detect truncation; JSON-safe the values, truncate long text, mask PII
  6. one audit line per call, written off the event loop
"""

from __future__ import annotations

import asyncio
import datetime as dt
import difflib
import json
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import asyncpg

from queryguard.db import get_pool, require_known_database
from queryguard.pii import MASK, is_pii_column, looks_like_email
from queryguard.safety import check_sql

# src/queryguard/executor.py -> parents[2] is the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The planner cost above which we refuse to run a query. Calibrated from evidence,
# not guessed: scripts/calibrate_explain.py ran EXPLAIN (FORMAT JSON) on the
# safe_sql of all 500 BIRD gold queries, as qg_reader on each query's own
# database. The maximum gold top-node Total Cost was 45515.75 (the `financial`
# database). This limit is 10x that maximum, rounded up to a round number, so
# every real gold query clears it with an order of magnitude to spare while the
# runaway cartesian products in tests/safety_cases/resource_attacks.yaml (whose
# costs run from ~1.8e6 to ~2.7e12) are refused before they execute. See
# logs/explain_calibration.csv for the full distribution. The limit is derived
# from gold alone and was not tuned against the attacks.
EXPLAIN_COST_LIMIT = 460_000

# Layer 3: a hard server-side ceiling on wall-clock time. Matches the project
# decision (all 500 gold queries finish in <=1.4s; p99 0.5s).
STATEMENT_TIMEOUT = "5s"

DEFAULT_MAX_ROWS = 100
# Layer 5: at most this many queries run at once for any one user, so a single
# caller cannot monopolise the pools.
MAX_CONCURRENT_PER_USER = 3
# Text cells longer than this are truncated before they leave the server.
MAX_CELL_CHARS = 200
_CELL_TRUNCATION_MARKER = "…[truncated]"

# PII masking lives in one shared module (queryguard.pii) so the executor's row
# output and the schema tools' sample values use exactly the same rule. The alias
# keeps the historical name used elsewhere in this package.
_is_pii_column = is_pii_column

# Substrings that mean "the database is unreachable" rather than "the query was
# wrong". Kept local so src/ never depends on the eval harness.
_UNREACHABLE_SIGNATURES = (
    "connection refused",
    "refused the network connection",
    "connection reset",
    "reset by peer",
    "forcibly closed",
    "cannot connect",
    "could not connect",
    "connection closed",
    "connection was closed",
    "server closed the connection",
    "connection does not exist",
)

# Per-user concurrency gates, each remembered with the loop it was created on.
# asyncio primitives bind to the loop that first uses them, so if the running
# loop has changed (e.g. a new test's event loop) we build a fresh semaphore
# rather than await a stale one. In the long-lived server there is exactly one
# loop, so this is simply one semaphore per user.
_semaphores: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}
# Cache of each database's table/view and column names, for "did you mean" hints.
_schema_cache: dict[str, list[str]] = {}

_QUOTED = re.compile(r'"([^"]+)"')


@dataclass(frozen=True)
class ExecResult:
    """The outcome of :func:`run_checked`.

    ``ok`` is True only on a successful execution. On any failure ``error`` holds
    one actionable, single-line message and ``error_kind`` classifies it:

      * ``"blocked"``   — check_sql rejected the SQL; the database was not touched.
      * ``"cost"``      — the planner estimate exceeded EXPLAIN_COST_LIMIT.
      * ``"timeout"``   — the query hit the server-side statement_timeout.
      * ``"sql_error"`` — a normal SQL error (bad name, not-allowed function, ...).
      * ``"unavailable"`` — the database looked unreachable.
    """

    ok: bool
    columns: list[str]
    rows: list[list[object]]
    row_count: int
    truncated: bool
    elapsed_ms: float
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    error_kind: str | None = None


# --------------------------------------------------------------------------- #
# Value conversion, text truncation, PII masking
# --------------------------------------------------------------------------- #
def _json_safe(value: object) -> object:
    """Convert one database value to something json.dumps can emit."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        # Decimal is JSON-foreign; floats are fine unless the value is NaN/Infinity.
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, memoryview):
        return f"<binary {value.nbytes} bytes>"
    if isinstance(value, (bytes, bytearray)):
        return f"<binary {len(value)} bytes>"
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return value  # left as-is per spec; asyncpg rarely hands back dicts
    # Anything we did not anticipate becomes its string form rather than leaking a
    # repr or breaking serialization downstream.
    return str(value)


def _truncate_text(value: object) -> object:
    """Cut an over-long text cell, marking that it was shortened."""
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + _CELL_TRUNCATION_MARKER
    return value


def _convert_rows(columns: list[str], records: list[asyncpg.Record]) -> list[list[object]]:
    """JSON-safe every cell, truncate long text, and mask PII (by column or value)."""
    pii = [is_pii_column(name) for name in columns]
    rows: list[list[object]] = []
    for record in records:
        row: list[object] = []
        for index, value in enumerate(record.values()):
            if pii[index] or looks_like_email(value):
                row.append(MASK)
            else:
                row.append(_truncate_text(_json_safe(value)))
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Concurrency, timing, warnings
# --------------------------------------------------------------------------- #
def _semaphore_for(user_id: str) -> asyncio.Semaphore:
    # No await between lookup and store, so concurrent coroutines on one loop
    # share the single semaphore this creates.
    loop = asyncio.get_running_loop()
    entry = _semaphores.get(user_id)
    if entry is None or entry[0] is not loop:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_PER_USER)
        _semaphores[user_id] = (loop, semaphore)
        return semaphore
    return entry[1]


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def _rewrite_warnings(check_warnings: list[str], truncated: bool, max_rows: int) -> list[str]:
    """Turn check_sql's R7 LIMIT notes into one AI-facing cap message.

    check_sql is called with max_rows + 1, so its own warnings mention that
    off-by-one number; we never surface it. If R7 changed the LIMIT (or the result
    was actually truncated) we say only that results are capped at ``max_rows``.
    """
    if truncated or any(warning.startswith("R7:") for warning in check_warnings):
        return [f"results are capped at {max_rows} rows"]
    return []


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #
def _first_line(message: str) -> str:
    return message.strip().splitlines()[0] if message.strip() else message.strip()


def _looks_unreachable(message: str) -> bool:
    low = message.lower()
    return any(signature in low for signature in _UNREACHABLE_SIGNATURES)


async def _schema_identifiers(database: str) -> list[str]:
    """All table/view and column names in a database's public schema (cached)."""
    cached = _schema_cache.get(database)
    if cached is not None:
        return cached
    pool = await get_pool(database)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.relname AS name FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v') "
            "UNION "
            "SELECT a.attname FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v') "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        )
    names = sorted({row["name"] for row in rows})
    _schema_cache[database] = names
    return names


async def _name_error_hint(database: str, message: str) -> str:
    """A sql_error hint for an undefined column/table, with closest-name suggestions."""
    first = _first_line(message)
    bad = _QUOTED.search(message)
    suggestions: list[str] = []
    if bad is not None:
        try:
            names = await _schema_identifiers(database)
            suggestions = difflib.get_close_matches(bad.group(1), names, n=3)
        except Exception:  # noqa: BLE001 - suggestions are best-effort; never mask the real error
            suggestions = []
    if suggestions:
        return f"{first}. Did you mean: {', '.join(suggestions)}? Check names with list_tables."
    return f"{first}. Check names with list_tables."


def _redact_db_error(message: str, sqlstate: str | None) -> str:
    """Strip any database value from a Postgres error before it leaves the server.

    SQLSTATE class 22 ("data exception") messages embed the offending value in
    quotes, e.g. ``invalid input syntax for type integer: "a@b.com"``. For those we
    keep only the text before the first quote character, so the value is never
    echoed. As a final backstop on *every* error, any whitespace-delimited token
    that looks like an email address is replaced with MASK.
    """
    if sqlstate is not None and sqlstate.startswith("22"):
        quote_positions = [pos for pos in (message.find('"'), message.find("'")) if pos != -1]
        if quote_positions:
            message = message[: min(quote_positions)].rstrip(" :")
    return " ".join(MASK if looks_like_email(token) else token for token in message.split())


async def _classify_error(exc: Exception, database: str, start: float) -> ExecResult:
    """Map a database/connection exception to an actionable ExecResult."""
    if isinstance(exc, asyncpg.QueryCanceledError):
        return _failure(
            "timeout",
            "query exceeded 5 seconds; add filters, aggregate, or narrow the query.",
            start,
        )
    if isinstance(exc, asyncpg.InsufficientPrivilegeError):
        return _failure("sql_error", "this function or table is not allowed.", start)
    if isinstance(exc, (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError)):
        return _failure("sql_error", await _name_error_hint(database, str(exc)), start)
    if isinstance(exc, asyncpg.PostgresError):
        message = _first_line(str(exc))
        if _looks_unreachable(message):
            return _failure("unavailable", f"database unreachable: {message}", start)
        return _failure(
            "sql_error", _redact_db_error(message, getattr(exc, "sqlstate", None)), start
        )
    # OSError / asyncpg InterfaceError: a transport-level failure to reach the db.
    return _failure("unavailable", f"database unreachable: {_first_line(str(exc))}", start)


def _failure(kind: str, message: str, start: float) -> ExecResult:
    return ExecResult(
        ok=False,
        columns=[],
        rows=[],
        row_count=0,
        truncated=False,
        elapsed_ms=_elapsed_ms(start),
        warnings=[],
        error=message,
        error_kind=kind,
    )


# --------------------------------------------------------------------------- #
# The database work
# --------------------------------------------------------------------------- #
async def _estimate_cost(conn: asyncpg.Connection, safe_sql: str) -> float:
    """The planner's top-node Total Cost for safe_sql (EXPLAIN only; no execution)."""
    rows = await conn.fetch("EXPLAIN (FORMAT JSON) " + safe_sql)
    plan = rows[0][0]
    if isinstance(plan, str):  # asyncpg returns the JSON column as text
        plan = json.loads(plan)
    return float(plan[0]["Plan"]["Total Cost"])


async def _fetch_rows(
    conn: asyncpg.Connection, safe_sql: str, max_rows: int
) -> tuple[list[str], list[asyncpg.Record], bool]:
    """Execute safe_sql with a cursor, fetching one extra row to detect truncation.

    Returns (column names, at most max_rows records, truncated). A prepared
    statement is used so the column names are known even when no rows come back.
    """
    statement = await conn.prepare(safe_sql)
    columns = [attr.name for attr in statement.get_attributes()]
    cursor = await statement.cursor()
    records = await cursor.fetch(max_rows + 1)
    truncated = len(records) > max_rows
    return columns, records[:max_rows], truncated


async def _cost_gate(conn: asyncpg.Connection, safe_sql: str, start: float) -> ExecResult | None:
    """The EXPLAIN cost gate: a "cost" ExecResult if over budget, else None.

    This step always runs — there is no parameter anywhere that can switch it off.
    It is only a *soft* estimate, though: the planner can badly under-cost a query
    (recursive CTEs especially), so a query that slips past here is still held by
    the hard 5s statement_timeout in :func:`_execute`.
    """
    cost = await _estimate_cost(conn, safe_sql)
    if cost > EXPLAIN_COST_LIMIT:
        return _failure(
            "cost",
            f"query is too expensive to run (estimated planner cost "
            f"{cost:,.0f} exceeds the limit of {EXPLAIN_COST_LIMIT:,}). "
            "Add filters, aggregate, or narrow the query.",
            start,
        )
    return None


async def _execute(
    database: str,
    safe_sql: str,
    check_warnings: list[str],
    max_rows: int,
    start: float,
) -> ExecResult:
    """Open a read-only transaction, gate on cost, then run the checked safe_sql.

    The cost gate and the execution share one transaction and one 5s timeout. The
    gate is unconditional; the only way to reach execution is to clear it.
    """
    try:
        pool = await get_pool(database)
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            over_budget = await _cost_gate(conn, safe_sql, start)
            if over_budget is not None:
                return over_budget
            columns, records, truncated = await _fetch_rows(conn, safe_sql, max_rows)
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        return await _classify_error(exc, database, start)

    rows = _convert_rows(columns, records)
    return ExecResult(
        ok=True,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=_elapsed_ms(start),
        warnings=_rewrite_warnings(check_warnings, truncated, max_rows),
        error=None,
        error_kind=None,
    )


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def _audit_path() -> Path:
    configured = os.environ.get("AUDIT_LOG_PATH")
    return Path(configured) if configured else PROJECT_ROOT / "logs" / "audit.jsonl"


def _append_audit_line(path: Path, line: str) -> None:
    """Blocking append of one audit line; call via asyncio.to_thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


async def _audit(user_id: str, database: str, safe_sql: str | None, result: ExecResult) -> None:
    """Append one JSON audit line per call, off the event loop. No secrets, ever."""
    record = {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "user_id": user_id,
        "database": database,
        "safe_sql": safe_sql,
        "status": "ok" if result.ok else result.error_kind,
        "elapsed_ms": result.elapsed_ms,
        "row_count": result.row_count,
        "truncated": result.truncated,
    }
    await asyncio.to_thread(_append_audit_line, _audit_path(), json.dumps(record) + "\n")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def run_checked(
    database: str,
    sql: str,
    user_id: str = "local",
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ExecResult:
    """Check ``sql`` and, if it passes, run it read-only against ``database``.

    Returns an :class:`ExecResult` in every case except an unknown database name,
    which raises :class:`queryguard.db.UnknownDatabaseError` before the SQL is even
    looked at. ``max_rows`` rows are returned at most; one extra row is fetched
    internally so ``truncated`` tells the caller whether more existed.
    """
    require_known_database(database)
    start = time.perf_counter()

    # check_sql is handed max_rows + 1 so the capped LIMIT still lets us fetch one
    # extra row and detect truncation. The database is not touched if it fails.
    check = check_sql(sql, max_rows=max_rows + 1)
    if not check.ok:
        result = _failure("blocked", "; ".join(check.errors), start)
        await _audit(user_id, database, None, result)
        return result

    assert check.safe_sql is not None  # ok implies safe_sql is set
    async with _semaphore_for(user_id):
        result = await _execute(database, check.safe_sql, check.warnings, max_rows, start)

    await _audit(user_id, database, check.safe_sql, result)
    return result
