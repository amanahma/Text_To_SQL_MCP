"""Schema-discovery tools: help an AI understand a database before it writes SQL.

Baseline eval failures were dominated by schema confusion — the model burned tool
calls on ``information_schema``, guessed cryptic BIRD column names (``soc`` =
"School Ownership Code", ``gsoffered`` = "grade span offered"), and tripped over
PostgreSQL rules (identifiers with spaces/capitals need double quotes; ``DESC``
sorts NULLs first). These four read-only tools — :func:`list_tables`,
:func:`describe_table`, :func:`find_columns`, :func:`validate_sql` — target that.

Safety invariants (mirrors the executor):
  * Every entry point validates ``database`` with ``require_known_database``.
  * Every catalog/sample query is written *by this module*, never from caller
    text. A caller-supplied table name is resolved against the catalog first; an
    unknown one raises :class:`SchemaLookupError` with difflib suggestions and no
    SQL is built from it. Identifiers that do reach a query come from the catalog
    (so, from the database) and are still quoted defensively with
    :func:`_quote_ident`.
  * Internal queries run as ``qg_reader`` in a read-only transaction with the same
    5s ``statement_timeout`` as the executor.
  * Results are cached per (user_id, database, ...) for 10 minutes.
  * Sample values are never returned for PII columns (the executor's rule).
"""

from __future__ import annotations

import difflib
import re
import time

import asyncpg

from queryguard.db import get_pool, require_known_database
from queryguard.executor import (
    EXPLAIN_COST_LIMIT,
    STATEMENT_TIMEOUT,
    _estimate_cost,
    _redact_db_error,
    _rewrite_warnings,
)
from queryguard.pii import MASK, is_pii_column, looks_like_email
from queryguard.safety import check_sql

CACHE_TTL_SECONDS = 600
FIND_COLUMNS_LIMIT = 20
FIND_COLUMNS_VALUES = 3
# difflib threshold for a fuzzy (non-substring) match in find_columns.
FUZZY_CUTOFF = 0.6

# A lower-case identifier that PostgreSQL accepts unquoted. Anything else (spaces,
# capitals, punctuation) must be double-quoted in SQL, so describe_table shows the
# exact quoted form to use.
_UNQUOTED_IDENT = re.compile(r"^[a-z_][a-z0-9_$]*$")

_SYSTEM_SCHEMA_FILTER = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'"
)


class SchemaLookupError(Exception):
    """A caller named a table/column that is not in the database's catalog."""

    def __init__(self, what: str, suggestions: list[str]) -> None:
        self.suggestions = suggestions
        message = f"{what!r} was not found in this database."
        if suggestions:
            message += f" Did you mean: {', '.join(suggestions)}?"
        else:
            message += " Call list_tables to see what exists."
        super().__init__(message)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _quote_ident(name: str) -> str:
    """Safely double-quote a SQL identifier (doubling any embedded quote)."""
    return '"' + name.replace('"', '""') + '"'


def _quoted_relation(schema: str, name: str) -> str:
    return f"{_quote_ident(schema)}.{_quote_ident(name)}"


def _needs_quoting(name: str) -> bool:
    return not _UNQUOTED_IDENT.match(name)


def _display(schema: str, name: str) -> str:
    """Schema-qualify a table name only when it is not in the public schema."""
    return name if schema == "public" else f"{schema}.{name}"


def _cut(value: object, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _distinct_estimate(n_distinct: float | None, est_rows: float) -> int | None:
    """pg_stats n_distinct: >=0 is a count; <0 is a negative fraction of the rows."""
    if n_distinct is None:
        return None
    if n_distinct >= 0:
        return int(n_distinct)
    return round(-n_distinct * max(int(est_rows), 0))


def _detail_level(n_columns: int) -> tuple[int, int, bool]:
    """(max sample values, value char cap, show stats) scaled to the column count.

    Keeps describe_table's output within ~1,500 tokens even on the widest BIRD
    table (european_football_2.match, 115 columns): a table that wide shows only
    name/type/flags and tells the model to pass ``columns=[...]`` to get stats and
    sample values for the handful it actually cares about.
    """
    if n_columns <= 25:
        return 5, 40, True
    if n_columns <= 60:
        return 3, 30, True
    return 0, 0, False


# --------------------------------------------------------------------------- #
# TTL cache
# --------------------------------------------------------------------------- #
_cache: dict[tuple, tuple[float, object]] = {}


def _cache_get(key: tuple) -> object | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    stored_at, value = hit
    if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: tuple, value: object) -> None:
    _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    """Drop every cached result (used by tests that mutate schemas)."""
    _cache.clear()


# --------------------------------------------------------------------------- #
# Internal query runner + catalog loaders (all SQL written here, never by caller)
# --------------------------------------------------------------------------- #
async def _run(database: str, query: str, *args: object) -> list[asyncpg.Record]:
    """Run one internal catalog/sample query as qg_reader, read-only, 5s timeout."""
    pool = await get_pool(database)
    async with pool.acquire() as conn, conn.transaction(readonly=True):
        await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        return await conn.fetch(query, *args)


_CATALOG_SQL = f"""
SELECT c.oid,
       n.nspname AS schema,
       c.relname AS name,
       c.reltuples AS est_rows,
       (SELECT count(*) FROM pg_attribute a
         WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped) AS col_count,
       obj_description(c.oid, 'pg_class') AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND {_SYSTEM_SCHEMA_FILTER}
ORDER BY n.nspname, c.relname
"""


async def _load_catalog(database: str, user_id: str) -> list[dict]:
    """Every user table in the database (cached), with row/column counts + comment."""
    key = (user_id, database, "catalog")
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    rows = await _run(database, _CATALOG_SQL)
    catalog = [
        {
            "oid": r["oid"],
            "schema": r["schema"],
            "name": r["name"],
            "display": _display(r["schema"], r["name"]),
            "est_rows": r["est_rows"],
            "col_count": r["col_count"],
            "comment": r["comment"],
        }
        for r in rows
    ]
    _cache_put(key, catalog)
    return catalog


def _resolve_table(catalog: list[dict], table: str) -> dict | None:
    """Match a caller's table name to a catalog entry (display name or bare name)."""
    for entry in catalog:
        if entry["display"] == table:
            return entry
    for entry in catalog:  # allow the bare name for public tables
        if entry["schema"] == "public" and entry["name"] == table:
            return entry
    return None


def _catalog_names(catalog: list[dict]) -> list[str]:
    return [entry["display"] for entry in catalog]


_COLUMNS_SQL = """
SELECT a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS type,
       NOT a.attnotnull AS nullable,
       col_description(a.attrelid, a.attnum) AS comment
FROM pg_attribute a
WHERE a.attrelid = $1 AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

_CONSTRAINTS_SQL = """
SELECT con.contype::text AS contype,
       att.attname AS col,
       cl2.relname AS ref_table,
       att2.attname AS ref_col
FROM pg_constraint con
JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
LEFT JOIN pg_class cl2 ON cl2.oid = con.confrelid
LEFT JOIN unnest(con.confkey) WITH ORDINALITY AS fk(attnum, ord) ON fk.ord = k.ord
LEFT JOIN pg_attribute att2 ON att2.attrelid = con.confrelid AND att2.attnum = fk.attnum
WHERE con.conrelid = $1 AND con.contype IN ('p', 'f')
"""

_STATS_SQL = """
SELECT attname, null_frac, n_distinct, most_common_vals::text::text[] AS mcv
FROM pg_stats
WHERE schemaname = $1 AND tablename = $2
"""


async def _constraints(database: str, oid: int) -> dict[str, dict]:
    """Per-column PK flag and FK target (``ref_table.ref_col``) for one table."""
    rows = await _run(database, _CONSTRAINTS_SQL, oid)
    out: dict[str, dict] = {}
    for r in rows:
        info = out.setdefault(r["col"], {})
        if r["contype"] == "p":
            info["pk"] = True
        elif r["contype"] == "f" and r["ref_table"] and r["ref_col"]:
            info["fk"] = f"{r['ref_table']}.{r['ref_col']}"
    return out


async def _table_stats(database: str, schema: str, name: str) -> dict[str, dict]:
    rows = await _run(database, _STATS_SQL, schema, name)
    return {
        r["attname"]: {"null_frac": r["null_frac"], "n_distinct": r["n_distinct"], "mcv": r["mcv"]}
        for r in rows
    }


async def _distinct_sample(database: str, schema: str, name: str, column: str, k: int) -> list:
    """Fallback sample when pg_stats has no row: SELECT DISTINCT col ... LIMIT k.

    Identifiers come from the catalog (not the caller) and are quoted; k is an int.
    """
    query = (
        f"SELECT DISTINCT {_quote_ident(column)} "
        f"FROM {_quoted_relation(schema, name)} LIMIT {int(k)}"
    )
    rows = await _run(database, query)
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# Tool 1: list_tables
# --------------------------------------------------------------------------- #
async def list_tables(database: str, user_id: str = "local") -> list[dict]:
    """Every user table: name (schema-qualified if not public), rows, columns, comment."""
    require_known_database(database)
    key = (user_id, database, "list_tables")
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    catalog = await _load_catalog(database, user_id)
    result = []
    for entry in catalog:
        item = {
            "table": entry["display"],
            "estimated_rows": max(int(entry["est_rows"]), 0),
            "columns": entry["col_count"],
        }
        if entry["comment"]:
            item["comment"] = entry["comment"]
        result.append(item)
    _cache_put(key, result)
    return result


# --------------------------------------------------------------------------- #
# Tool 2: describe_table
# --------------------------------------------------------------------------- #
async def describe_table(
    database: str,
    table: str,
    columns: list[str] | None = None,
    user_id: str = "local",
) -> dict:
    """Describe one table's columns: type, nullability, keys, comments, stats, samples."""
    require_known_database(database)
    key = (user_id, database, "describe", table, tuple(columns) if columns else None)
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    catalog = await _load_catalog(database, user_id)
    entry = _resolve_table(catalog, table)
    if entry is None:
        raise SchemaLookupError(
            table, difflib.get_close_matches(table, _catalog_names(catalog), n=3)
        )

    all_cols = await _run(database, _COLUMNS_SQL, entry["oid"])
    if columns:
        wanted = set(columns)
        selected = [c for c in all_cols if c["name"] in wanted]
        if not selected:
            names = [c["name"] for c in all_cols]
            raise SchemaLookupError(columns[0], difflib.get_close_matches(columns[0], names, n=3))
    else:
        selected = list(all_cols)

    constraints = await _constraints(database, entry["oid"])
    stats = await _table_stats(database, entry["schema"], entry["name"])
    max_values, value_len, show_stats = _detail_level(len(selected))

    columns_out: list[dict] = []
    for col in selected:
        name = col["name"]
        out: dict = {"name": name, "type": col["type"]}
        if _needs_quoting(name):
            out["quoted"] = _quote_ident(name)  # paste this exact form into SQL
        con = constraints.get(name, {})
        if con.get("pk"):
            out["primary_key"] = True
        if con.get("fk"):
            out["foreign_key"] = con["fk"]
        if col["comment"]:
            out["comment"] = col["comment"]

        # Nullability and stats are per-column detail; on very wide tables they are
        # dropped (show_stats is False) to stay within the output budget, and the
        # model is told to pass columns=[...] to get them.
        stat = stats.get(name)
        if show_stats:
            out["nullable"] = col["nullable"]
            if stat is not None:
                out["null_fraction"] = round(stat["null_frac"], 4)
                out["distinct_estimate"] = _distinct_estimate(stat["n_distinct"], entry["est_rows"])

        if is_pii_column(name):
            out["pii_masked"] = True  # values are deliberately withheld
        elif max_values > 0:
            values = await _sample_values(database, entry, name, stat, max_values, value_len)
            if values:
                out["sample_values"] = values
        columns_out.append(out)

    result: dict = {
        "table": entry["display"],
        "estimated_rows": max(int(entry["est_rows"]), 0),
        "column_count": entry["col_count"],
        "columns": columns_out,
    }
    if entry["comment"]:
        result["comment"] = entry["comment"]
    if not show_stats and not columns:
        result["note"] = (
            "Many columns: nullability, stats and sample values omitted. Re-call with "
            "columns=[...] for full detail on specific columns."
        )
    _cache_put(key, result)
    return result


async def _sample_values(
    database: str,
    entry: dict,
    column: str,
    stat: dict | None,
    max_values: int,
    value_len: int,
) -> list[str]:
    """Up to max_values sample values: pg_stats first, else a DISTINCT fallback."""
    if stat is not None and stat["mcv"]:
        raw = stat["mcv"][:max_values]
    else:
        raw = await _distinct_sample(database, entry["schema"], entry["name"], column, max_values)
    # Value-based masking too: an email-looking value is hidden even here.
    return [MASK if looks_like_email(v) else _cut(v, value_len) for v in raw]


# --------------------------------------------------------------------------- #
# Tool 3: find_columns
# --------------------------------------------------------------------------- #
_ALL_COLUMNS_SQL = f"""
SELECT n.nspname AS schema,
       c.relname AS name,
       a.attname AS column,
       format_type(a.atttypid, a.atttypmod) AS type,
       col_description(a.attrelid, a.attnum) AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind IN ('r', 'p') AND {_SYSTEM_SCHEMA_FILTER}
ORDER BY n.nspname, c.relname, a.attnum
"""

_DB_STATS_SQL = """
SELECT schemaname, tablename, attname, most_common_vals::text::text[] AS mcv
FROM pg_stats
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  AND schemaname NOT LIKE 'pg_toast%'
"""


async def find_columns(database: str, keyword: str, user_id: str = "local") -> list[dict]:
    """Find up to 20 columns matching a keyword (substring + fuzzy) across all tables."""
    require_known_database(database)
    key = (user_id, database, "find", keyword.lower())
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    rows = await _run(database, _ALL_COLUMNS_SQL)
    needle = keyword.lower().strip()
    scored: list[tuple[int, float, dict]] = []
    for r in rows:
        column = r["column"]
        display = _display(r["schema"], r["name"])
        comment = r["comment"] or ""
        haystacks = (column.lower(), display.lower(), comment.lower())
        substring = any(needle in h for h in haystacks)
        ratio = max(
            difflib.SequenceMatcher(None, needle, column.lower()).ratio(),
            difflib.SequenceMatcher(None, needle, r["name"].lower()).ratio(),
        )
        if substring:
            score = 2
        elif ratio >= FUZZY_CUTOFF:
            score = 1
        else:
            continue
        scored.append(
            (
                score,
                ratio,
                {
                    "schema": r["schema"],
                    "name": r["name"],
                    "display": display,
                    "column": column,
                    "type": r["type"],
                },
            )
        )

    scored.sort(key=lambda s: (-s[0], -s[1], s[2]["display"], s[2]["column"]))
    top = scored[:FIND_COLUMNS_LIMIT]

    db_stats = await _database_stats(database)
    result = []
    for _score, _ratio, c in top:
        item = {"table": c["display"], "column": c["column"], "type": c["type"]}
        if not is_pii_column(c["column"]):
            mcv = db_stats.get((c["schema"], c["name"], c["column"]))
            if mcv:
                # Value-based masking too: an email-looking value is hidden even in
                # a column whose name does not look like PII (same rule as the
                # executor and describe_table).
                item["sample_values"] = [
                    MASK if looks_like_email(v) else _cut(v, 40) for v in mcv[:FIND_COLUMNS_VALUES]
                ]
        result.append(item)
    _cache_put(key, result)
    return result


async def _database_stats(database: str) -> dict[tuple[str, str, str], list]:
    rows = await _run(database, _DB_STATS_SQL)
    return {(r["schemaname"], r["tablename"], r["attname"]): r["mcv"] for r in rows if r["mcv"]}


# --------------------------------------------------------------------------- #
# Tool 4: validate_sql
# --------------------------------------------------------------------------- #
async def validate_sql(database: str, sql: str, user_id: str = "local") -> dict:
    """Check SQL and estimate its planner cost WITHOUT executing it."""
    require_known_database(database)
    check = check_sql(sql, max_rows=100)
    if not check.ok:
        return {
            "ok": False,
            "errors": check.errors,
            "warnings": [],
            "final_sql": None,
            "estimated_cost": None,
            "cost_limit": EXPLAIN_COST_LIMIT,
            "within_cost_limit": None,
        }

    warnings = _rewrite_warnings(check.warnings, False, 100)
    assert check.safe_sql is not None
    try:
        pool = await get_pool(database)
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            cost = await _estimate_cost(conn, check.safe_sql)
    except asyncpg.PostgresError as exc:
        # The SQL is safe but could not be planned (e.g. an unknown column, or a
        # constant-folded data exception); report it without executing anything and
        # with any database value redacted from the error text.
        return {
            "ok": False,
            "errors": [_redact_db_error(str(exc).splitlines()[0], getattr(exc, "sqlstate", None))],
            "warnings": warnings,
            "final_sql": check.safe_sql,
            "estimated_cost": None,
            "cost_limit": EXPLAIN_COST_LIMIT,
            "within_cost_limit": None,
        }

    return {
        "ok": True,
        "errors": [],
        "warnings": warnings,
        "final_sql": check.safe_sql,
        "estimated_cost": round(cost, 2),
        "cost_limit": EXPLAIN_COST_LIMIT,
        "within_cost_limit": cost <= EXPLAIN_COST_LIMIT,
    }
