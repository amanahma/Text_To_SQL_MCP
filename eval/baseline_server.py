"""The baseline MCP server — condition A of the experiment.

This is the "dumb but honest" comparison point: a single ``run_sql`` tool whose
description is the database's full schema, and whose only protection is the
read-only ``qg_reader`` role (plus a per-statement timeout and a row cap). There
are deliberately **no** SQL safety checks here — that is what the main server
adds, and what the evaluation is meant to measure.

``build_baseline_server`` binds one server to one database. The schema text is
baked into the tool description at build time, so the model sees the whole schema
up front and never needs a discovery tool.

Frozen after Phase 1: do not add tools, hints, or sample values here.
"""

from __future__ import annotations

from typing import Any

import asyncpg
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from queryguard.db import get_pool

ROW_CAP = 100
STATEMENT_TIMEOUT = "5s"


async def _fetch_schema_text(db_id: str) -> str:
    """Render the full public-schema layout of ``db_id`` as plain text.

    Tables with their columns and types, primary keys, and foreign keys — the
    minimum a model needs to write correct SQL, and nothing more.
    """
    pool = await get_pool(db_id)
    async with pool.acquire() as conn:
        tables = [
            r["relname"]
            for r in await conn.fetch(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "ORDER BY c.relname"
            )
        ]
        column_rows = await conn.fetch(
            "SELECT c.relname AS table_name, a.attname AS column_name, "
            "       format_type(a.atttypid, a.atttypmod) AS data_type "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "      AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY c.relname, a.attnum"
        )
        pk_rows = await conn.fetch(
            "SELECT conrelid::regclass::text AS table_name, "
            "       pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint "
            "WHERE contype = 'p' AND connamespace = 'public'::regnamespace"
        )
        fk_rows = await conn.fetch(
            "SELECT conrelid::regclass::text AS table_name, "
            "       pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint "
            "WHERE contype = 'f' AND connamespace = 'public'::regnamespace "
            "ORDER BY conrelid::regclass::text, conname"
        )

    columns_by_table: dict[str, list[str]] = {}
    for row in column_rows:
        columns_by_table.setdefault(row["table_name"], []).append(
            f"    {row['column_name']} {row['data_type']}"
        )
    pk_by_table: dict[str, str] = {r["table_name"]: r["definition"] for r in pk_rows}
    fk_by_table: dict[str, list[str]] = {}
    for row in fk_rows:
        fk_by_table.setdefault(row["table_name"], []).append(row["definition"])

    blocks: list[str] = [f"PostgreSQL database {db_id!r}. Tables in schema public:\n"]
    for table in tables:
        lines = [f"Table {table}"]
        lines.extend(columns_by_table.get(table, []))
        if table in pk_by_table:
            lines.append(f"    {pk_by_table[table]}")
        for fk in fk_by_table.get(table, []):
            lines.append(f"    {fk}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _to_jsonable(value: Any) -> Any:
    """JSON-safe form of a database value; anything exotic becomes ``str()``."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


async def build_baseline_server(db_id: str) -> FastMCP:
    """Build a baseline MCP server bound to a single BIRD database.

    The returned server exposes exactly one tool, ``run_sql``, whose description
    is the full schema of ``db_id``.
    """
    schema_text = await _fetch_schema_text(db_id)
    description = (
        "Run a single read-only SQL query against this PostgreSQL database and "
        f"get up to {ROW_CAP} result rows back as JSON (keys 'columns' and 'rows'). "
        f"Queries run as a read-only role with a {STATEMENT_TIMEOUT} timeout. "
        "Use PostgreSQL syntax. Identifiers are case-sensitive when quoted.\n\n"
        f"{schema_text}"
    )

    mcp = FastMCP(name=f"queryguard-baseline-{db_id}")

    @mcp.tool(
        name="run_sql",
        description=description,
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def run_sql(sql: str) -> dict[str, Any]:
        pool = await get_pool(db_id)
        try:
            async with pool.acquire() as conn, conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                stmt = await conn.prepare(sql)
                columns = [attr.name for attr in stmt.get_attributes()]
                rows: list[list[Any]] = []
                async for record in stmt.cursor():
                    rows.append([_to_jsonable(v) for v in record])
                    if len(rows) >= ROW_CAP:
                        break
        except asyncpg.PostgresError as exc:
            # The only protection is the read-only role; surface the raw error so
            # the model can fix its query.
            raise ToolError(str(exc)) from exc
        return {"columns": columns, "rows": rows}

    return mcp
