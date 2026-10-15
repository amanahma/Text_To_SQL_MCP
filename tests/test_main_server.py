"""The main QueryGuard server's discovery tools, via an in-memory MCP client."""

from __future__ import annotations

import pytest
from fastmcp import Client

from queryguard.db import allowed_databases
from queryguard.server import build_server

EXPECTED_DB_COUNT = 11

# execute_query's audit lines are redirected to a per-test temp file by the
# autouse fixture in tests/conftest.py.


async def test_list_databases_returns_all_configured() -> None:
    expected = allowed_databases()
    assert len(expected) == EXPECTED_DB_COUNT
    server = build_server()
    async with Client(server) as client:
        result = await client.call_tool("list_databases", {}, raise_on_error=False)
    assert result.is_error is False
    assert sorted(result.data) == sorted(expected)


@pytest.mark.slow
async def test_list_tables_returns_rich_entries() -> None:
    server = build_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "list_tables", {"database": "superhero"}, raise_on_error=False
        )
    assert result.is_error is False
    names = {entry["table"] for entry in result.data}
    assert {"superhero", "alignment"} <= names
    superhero = next(e for e in result.data if e["table"] == "superhero")
    assert superhero["estimated_rows"] > 0
    assert superhero["columns"] > 0


async def test_list_tables_unknown_database_is_tool_error() -> None:
    server = build_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "list_tables", {"database": "not_a_real_db"}, raise_on_error=False
        )
    assert result.is_error is True
    assert "Unknown database" in result.content[0].text


async def test_all_tools_are_read_only() -> None:
    server = build_server()
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {
        "list_databases",
        "list_tables",
        "describe_table",
        "find_columns",
        "validate_sql",
        "execute_query",
    }
    for tool in tools.values():
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


@pytest.mark.slow
async def test_execute_query_success_returns_structured_rows() -> None:
    server = build_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "execute_query",
            {"database": "superhero", "sql": "SELECT count(*) AS n FROM superhero"},
            raise_on_error=False,
        )
    assert result.is_error is False
    data = result.data
    assert data["columns"] == ["n"]
    assert data["row_count"] == 1
    assert data["truncated"] is False
    assert isinstance(data["rows"][0][0], int)
    assert "elapsed_ms" in data and "warnings" in data


async def test_execute_query_blocked_is_tool_error() -> None:
    server = build_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "execute_query",
            {"database": "superhero", "sql": "DROP TABLE superhero"},
            raise_on_error=False,
        )
    assert result.is_error is True
    # The message is prefixed with the error_kind so the model can act on it.
    assert result.content[0].text.startswith("blocked:")


async def test_execute_query_unknown_database_is_tool_error() -> None:
    server = build_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "execute_query",
            {"database": "not_a_real_db", "sql": "SELECT 1"},
            raise_on_error=False,
        )
    assert result.is_error is True
    assert "Unknown database" in result.content[0].text
