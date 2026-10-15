"""queryguard.db: database allow-listing and per-database pool caching."""

from __future__ import annotations

import pytest

from queryguard import db

# Keep total pool capacity this far below the server's max_connections.
CONNECTION_SAFETY_MARGIN = 10


async def test_unknown_database_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIRD_DBS", "alpha,beta")
    with pytest.raises(db.UnknownDatabaseError):
        await db.get_pool("gamma")


async def test_pool_is_created_once_per_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIRD_DBS", "alpha,beta")
    monkeypatch.setenv("READER_PASSWORD", "unused-in-fake")

    created: list[str] = []

    class FakePool:
        def __init__(self, database: str) -> None:
            self.database = database

        async def close(self) -> None:
            pass

    async def fake_create_pool(**kwargs: object) -> FakePool:
        created.append(str(kwargs["database"]))
        return FakePool(str(kwargs["database"]))

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    db._pools.clear()

    alpha_first = await db.get_pool("alpha")
    alpha_again = await db.get_pool("alpha")
    beta = await db.get_pool("beta")

    # Same name reuses the pool; different names get different pools.
    assert alpha_first is alpha_again
    assert alpha_first is not beta
    # Each database's pool was created exactly once.
    assert created == ["alpha", "beta"]


@pytest.mark.slow
async def test_total_pool_capacity_under_max_connections() -> None:
    """One pool per database must not, in total, exhaust the server's connections.

    Opens a real pool for all 11 databases, reads SHOW max_connections from the
    server, and asserts the sum of every pool's max_size stays below
    max_connections minus a safety margin. With max_size=5 and 11 databases that is
    55, comfortably under 100 - 10 = 90.
    """
    names = db.allowed_databases()
    pools = [await db.get_pool(name) for name in names]

    async with pools[0].acquire() as conn:
        max_connections = int(await conn.fetchval("SHOW max_connections"))

    # Every pool is sized as configured.
    assert all(pool.get_max_size() == db.POOL_MAX_SIZE for pool in pools)
    assert all(pool.get_min_size() == db.POOL_MIN_SIZE for pool in pools)

    capacity = sum(pool.get_max_size() for pool in pools)
    assert capacity == len(names) * db.POOL_MAX_SIZE
    assert capacity <= max_connections - CONNECTION_SAFETY_MARGIN, (
        f"total pool capacity {capacity} is within {CONNECTION_SAFETY_MARGIN} of "
        f"max_connections {max_connections}"
    )
