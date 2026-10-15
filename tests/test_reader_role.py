"""The qg_reader role must be able to read and nothing else.

This is QueryGuard's primary safety layer, so we assert it directly against the
live database: SELECT works; every kind of write is rejected; sessions are
read-only by default.
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from conftest import BIRD_DBS

# Run every check in this module against (at least) two different databases, to
# prove the read-only guarantee holds per database, not just on one.
pytestmark = pytest.mark.parametrize("reader_conn", BIRD_DBS[:2], indirect=True)


@pytest_asyncio.fixture
async def sample_table(reader_conn: asyncpg.Connection) -> str:
    """Name of a real, loaded BIRD table in the public schema."""
    row = await reader_conn.fetchrow(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name LIMIT 1"
    )
    assert row is not None, "No BIRD tables are loaded; run scripts/load_bird.py."
    return row["table_name"]


@pytest_asyncio.fixture
async def sample_column(reader_conn: asyncpg.Connection, sample_table: str) -> str:
    row = await reader_conn.fetchrow(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1 "
        "ORDER BY ordinal_position LIMIT 1",
        sample_table,
    )
    assert row is not None
    return row["column_name"]


async def test_select_works(reader_conn: asyncpg.Connection, sample_table: str) -> None:
    rows = await reader_conn.fetch(f'SELECT * FROM "{sample_table}" LIMIT 1')
    assert isinstance(rows, list)


async def test_session_is_read_only(reader_conn: asyncpg.Connection) -> None:
    value = await reader_conn.fetchval("SHOW default_transaction_read_only")
    assert value == "on"


async def test_insert_rejected(reader_conn: asyncpg.Connection, sample_table: str) -> None:
    with pytest.raises(asyncpg.PostgresError):
        await reader_conn.execute(f'INSERT INTO "{sample_table}" DEFAULT VALUES')


async def test_update_rejected(
    reader_conn: asyncpg.Connection, sample_table: str, sample_column: str
) -> None:
    with pytest.raises(asyncpg.PostgresError):
        await reader_conn.execute(
            f'UPDATE "{sample_table}" SET "{sample_column}" = "{sample_column}" WHERE false'
        )


async def test_delete_rejected(reader_conn: asyncpg.Connection, sample_table: str) -> None:
    with pytest.raises(asyncpg.PostgresError):
        await reader_conn.execute(f'DELETE FROM "{sample_table}" WHERE false')


async def test_truncate_rejected(reader_conn: asyncpg.Connection, sample_table: str) -> None:
    with pytest.raises(asyncpg.PostgresError):
        await reader_conn.execute(f'TRUNCATE TABLE "{sample_table}"')


async def test_drop_table_rejected(reader_conn: asyncpg.Connection, sample_table: str) -> None:
    with pytest.raises(asyncpg.PostgresError):
        await reader_conn.execute(f'DROP TABLE "{sample_table}"')


async def test_create_table_rejected(reader_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.PostgresError):
        await reader_conn.execute("CREATE TABLE qg_should_not_exist (id integer)")


async def test_create_temp_table_rejected(reader_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.PostgresError):
        await reader_conn.execute("CREATE TEMP TABLE qg_tmp_should_not_exist (id integer)")
