"""Shared read-only access to the BIRD databases.

Every component (the main server, the baseline server, the eval harness) reaches
PostgreSQL through here, always as the ``qg_reader`` role. We keep one lazily
created :class:`asyncpg.Pool` per database so repeated calls reuse connections.

Configuration comes from the ``.env`` at the project root. We locate it relative
to *this file* rather than the current working directory, because Claude Desktop
starts the server from some other directory entirely. ``load_dotenv`` does not
override variables already present in the real environment (handy for CI).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

# src/queryguard/db.py -> parents[2] is the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class UnknownDatabaseError(ValueError):
    """Raised when a caller asks for a database that is not in BIRD_DBS."""


# Per-database pool sizing. We keep one pool per BIRD database (11 of them), and
# the server's max_connections is 100, so the total capacity across every pool
# must stay well under that. asyncpg's DEFAULT max_size is 10 -> 11 * 10 = 110
# connections, which would blow past max_connections. We size each pool
# explicitly instead:
#   * max_size=5  -> 11 * 5 = 55 total, comfortably under 100 (and under the
#     100 - 10 safety margin of 90). 5 also sits above the executor's per-user
#     concurrency cap of 3, leaving headroom for the eval's scoring connection.
#   * min_size=1  -> each created pool keeps one warm connection, so a database
#     that has gone idle does not pay a full reconnect on its next query. 11
#     always-open connections is a negligible share of 100.
# See tests/test_db.py::test_total_pool_capacity_under_max_connections.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5

# One pool per database name, created on first use.
_pools: dict[str, asyncpg.Pool] = {}
# Guards pool creation so two coroutines racing on the same database make one pool.
_pools_lock = asyncio.Lock()


def allowed_databases() -> list[str]:
    """Return the configured BIRD database names, in declared order.

    Read fresh from the environment every call so tests can override BIRD_DBS.
    """
    raw = os.environ.get("BIRD_DBS", "")
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if not names:
        raise RuntimeError("BIRD_DBS is not set. Copy .env.example to .env and fill it in.")
    return names


def require_known_database(db_id: str) -> None:
    """Raise :class:`UnknownDatabaseError` unless ``db_id`` is in BIRD_DBS."""
    allowed = allowed_databases()
    if db_id not in allowed:
        raise UnknownDatabaseError(
            f"Unknown database {db_id!r}. Available databases: {', '.join(allowed)}."
        )


def _reader_connect_kwargs(db_id: str) -> dict[str, object]:
    password = os.environ.get("READER_PASSWORD")
    if not password:
        raise RuntimeError("READER_PASSWORD is not set. Copy .env.example to .env and fill it in.")
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5433")),
        "user": os.environ.get("READER_USER", "qg_reader"),
        "password": password,
        "database": db_id,
    }


async def get_pool(db_id: str) -> asyncpg.Pool:
    """Return the (lazily created) connection pool for one BIRD database.

    The database name is validated against BIRD_DBS first, so an unknown name
    never reaches asyncpg.
    """
    require_known_database(db_id)
    pool = _pools.get(db_id)
    if pool is not None:
        return pool
    async with _pools_lock:
        # Re-check inside the lock: another coroutine may have created it.
        pool = _pools.get(db_id)
        if pool is None:
            pool = await asyncpg.create_pool(
                min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, **_reader_connect_kwargs(db_id)
            )
            _pools[db_id] = pool
        return pool


async def close_all() -> None:
    """Close every open pool. Call once on shutdown."""
    async with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        await pool.close()
