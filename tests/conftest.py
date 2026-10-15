"""Shared test fixtures: connection settings from .env and qg_reader connections.

After the Phase 0.5 split there are 11 databases (one per BIRD db_id), listed in
BIRD_DBS. The ``reader_conn`` fixture connects as qg_reader to one of them; which
one can be chosen via indirect parametrization, defaulting to the first.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
_ENV = dotenv_values(PROJECT_ROOT / ".env")


def setting(key: str, default: str | None = None) -> str:
    """Read a config value from .env, falling back to the process env then a default."""
    value = _ENV.get(key) or os.environ.get(key) or default
    if value is None:
        pytest.fail(f"Missing {key}; copy .env.example to .env and fill it in.")
    return value


def bird_databases() -> list[str]:
    databases = [name.strip() for name in setting("BIRD_DBS").split(",") if name.strip()]
    if not databases:
        pytest.fail("BIRD_DBS is empty in .env.")
    return sorted(databases)


BIRD_DBS = bird_databases()


def reader_connect_kwargs(database: str) -> dict[str, object]:
    return {
        "host": setting("DB_HOST", "localhost"),
        "port": int(setting("DB_PORT", "5433")),
        "user": setting("READER_USER", "qg_reader"),
        "password": setting("READER_PASSWORD"),
        "database": database,
    }


async def connect_as_reader(database: str) -> asyncpg.Connection:
    """Connect as qg_reader to a specific database, or fail with setup instructions."""
    try:
        return await asyncpg.connect(**reader_connect_kwargs(database))
    except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
        pytest.fail(
            f"Could not connect to '{database}' as the reader role.\n"
            "Make sure the stack is up and built:\n"
            "    docker compose up -d\n"
            "    uv run python scripts/load_bird.py\n"
            "    uv run python scripts/split_bird.py\n"
            "    uv run python scripts/create_reader.py\n"
            f"(underlying error: {exc})"
        )


def admin_connect_kwargs(database: str) -> dict[str, object]:
    return {
        "host": setting("DB_HOST", "localhost"),
        "port": int(setting("DB_PORT", "5433")),
        "user": "postgres",
        "password": setting("POSTGRES_PASSWORD"),
        "database": database,
    }


async def connect_as_admin(database: str) -> asyncpg.Connection:
    """Connect as the postgres superuser (for test-only DDL fixtures)."""
    try:
        return await asyncpg.connect(**admin_connect_kwargs(database))
    except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
        pytest.fail(
            f"Could not connect to '{database}' as postgres. Is the stack up "
            f"(docker compose up -d)? (underlying error: {exc})"
        )


@pytest_asyncio.fixture
async def reader_conn(request: pytest.FixtureRequest) -> asyncpg.Connection:
    database = getattr(request, "param", None) or BIRD_DBS[0]
    conn = await connect_as_reader(database)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the executor's audit log at a per-test temp file.

    The executor appends one JSON line per run_checked call to AUDIT_LOG_PATH
    (default logs/audit.jsonl). Without this, any test that calls run_checked —
    directly or through the MCP tool — would write into the real audit log. This
    autouse fixture redirects every test to its own tmp file, so no test can ever
    pollute logs/audit.jsonl. Tests that need to read the log use the same
    ``tmp_path / "audit.jsonl"``.
    """
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))


@pytest_asyncio.fixture(autouse=True)
async def _close_queryguard_pools() -> None:
    """Close any queryguard.db pools after each test.

    pytest-asyncio gives each test its own event loop; an asyncpg pool cached in
    queryguard.db is bound to the loop that created it, so it must not survive
    into the next test. Closing (and clearing the cache) after every test keeps
    each test's pools loop-local.
    """
    from queryguard import db

    yield
    await db.close_all()
