"""Resource attacks must always be *contained*, never run away.

Every case in tests/safety_cases/resource_attacks.yaml passes check_sql (it is a
structurally valid read query), so Phase 3's job is to stop it from exhausting
the server. There are three containment mechanisms, and a case may be caught by
any of them:

  * the EXPLAIN cost gate refuses it before execution  -> error_kind "cost"
  * the 5s statement_timeout cancels it mid-flight      -> error_kind "timeout"
  * the LIMIT/row cap turns it into a cheap, bounded query (e.g. ORDER BY ...
    LIMIT 101 becomes a top-N, a COUNT over a small table just finishes) -> ok,
    at most 100 rows

So the invariant this test enforces is: for every attack, run_checked returns
quickly (within ~6s), never raises, never yields more than 100 rows, and ends in
a *contained* state (cost / timeout / a row-capped success) — never a
sql_error, unavailable, or an uncapped result.

NOTE for the owner: on the current BIRD data, 6 of the 11 cases are refused by
the cost gate and the other 5 are contained by the LIMIT/row cap rather than by
the timeout (they finish in <2.5s). Which mechanism catches the borderline cases
(the heavy sort, the regex) is machine-dependent, so this test deliberately
asserts containment rather than a fixed "timeout or cost" outcome per case.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import sqlglot
import yaml

from queryguard.db import allowed_databases, get_pool
from queryguard.executor import run_checked

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ATTACKS = PROJECT_ROOT / "tests" / "safety_cases" / "resource_attacks.yaml"
DEFAULT_DB = "california_schools"
MAX_ELAPSED_MS = 6500  # ~6s: the 5s timeout plus connection/overhead headroom

CASES = yaml.safe_load(RESOURCE_ATTACKS.read_text(encoding="utf-8"))


async def _tables_by_database() -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for db_id in allowed_databases():
        pool = await get_pool(db_id)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
        index[db_id] = {row["relname"].lower() for row in rows}
    return index


def _referenced_tables(sql: str) -> set[str]:
    tree = sqlglot.parse_one(sql, read="postgres")
    return {t.name.lower() for t in tree.find_all(sqlglot.exp.Table) if t.name}


def _database_for(sql: str, index: dict[str, set[str]]) -> str:
    wanted = _referenced_tables(sql)
    if not wanted:
        return DEFAULT_DB
    for db_id, tables in index.items():
        if wanted <= tables:
            return db_id
    return DEFAULT_DB


@pytest.mark.slow
async def test_every_resource_attack_is_contained() -> None:
    index = await _tables_by_database()

    async def run_one(case: dict) -> tuple[str, object]:
        # A distinct user_id per case so the per-user semaphore does not serialise
        # them; all cases run concurrently and the whole test stays within seconds.
        result = await run_checked(case["db"], case["sql"], user_id=case["id"])
        return case["id"], result

    cases = [{**c, "db": _database_for(c["sql"], index)} for c in CASES]
    start = time.perf_counter()
    results = await asyncio.gather(*(run_one(c) for c in cases))  # a raise here fails the test
    wall = time.perf_counter() - start

    contained = {None, "cost", "timeout"}  # ok (row-capped), refused, or cancelled
    for case_id, result in results:
        assert result.error_kind in contained, (
            f"{case_id}: not contained (error_kind={result.error_kind!r}, error={result.error!r})"
        )
        assert result.row_count <= 100, f"{case_id}: returned {result.row_count} rows"
        assert len(result.rows) <= 100, f"{case_id}: returned {len(result.rows)} rows"
        assert result.elapsed_ms < MAX_ELAPSED_MS, f"{case_id}: took {result.elapsed_ms}ms"

    # At least some case is refused outright by the cost gate (the extreme products).
    assert any(result.error_kind == "cost" for _, result in results)
    # Concurrency keeps the whole suite well under a serial worst case.
    assert wall < 15.0
