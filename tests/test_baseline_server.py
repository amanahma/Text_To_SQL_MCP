"""The baseline server (condition A), checked against the live superhero database.

Requires the stack to be up (docker compose up -d + the load/split/reader scripts),
exactly like the other live-database tests.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from eval.baseline_server import ROW_CAP, build_baseline_server
from queryguard import db

DB_ID = "superhero"


async def _superhero_tables_and_columns() -> dict[str, list[str]]:
    pool = await db.get_pool(DB_ID)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.relname AS table_name, a.attname AS column_name "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "      AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY c.relname, a.attnum"
        )
    schema: dict[str, list[str]] = {}
    for row in rows:
        schema.setdefault(row["table_name"], []).append(row["column_name"])
    return schema


@pytest.mark.slow
async def test_description_lists_every_table_and_column() -> None:
    schema = await _superhero_tables_and_columns()
    assert schema, "superhero has no tables; is the data loaded?"

    server = await build_baseline_server(DB_ID)
    async with Client(server) as client:
        tools = await client.list_tools()
    assert [t.name for t in tools] == ["run_sql"]
    description = tools[0].description or ""

    for table, columns in schema.items():
        assert f"Table {table}" in description, f"missing table {table}"
        for column in columns:
            assert column in description, f"missing column {table}.{column}"


@pytest.mark.slow
async def test_run_sql_caps_rows_at_100() -> None:
    server = await build_baseline_server(DB_ID)
    async with Client(server) as client:
        result = await client.call_tool(
            "run_sql", {"sql": "SELECT id FROM superhero"}, raise_on_error=False
        )
    assert result.is_error is False
    assert len(result.data["rows"]) == ROW_CAP


@pytest.mark.slow
async def test_run_sql_sets_5s_statement_timeout() -> None:
    server = await build_baseline_server(DB_ID)
    async with Client(server) as client:
        result = await client.call_tool(
            "run_sql", {"sql": "SHOW statement_timeout"}, raise_on_error=False
        )
    assert result.is_error is False
    # SET LOCAL statement_timeout = '5s' is in force inside the query's transaction.
    assert result.data["rows"][0][0] in ("5s", "5000ms")


@pytest.mark.slow
async def test_write_attempt_returns_tool_error() -> None:
    server = await build_baseline_server(DB_ID)
    async with Client(server) as client:
        result = await client.call_tool(
            "run_sql",
            {"sql": "CREATE TABLE qg_should_not_exist (id integer)"},
            raise_on_error=False,
        )
    assert result.is_error is True
    assert result.content[0].text  # a non-empty error message the model can act on


async def test_unknown_database_cannot_build_server() -> None:
    with pytest.raises(db.UnknownDatabaseError):
        await build_baseline_server("definitely_not_a_database")
