"""The loaded BIRD data must match the questions file, one database per db_id.

After the Phase 0.5 split, each BIRD db_id is its own PostgreSQL database and every
question runs only against its own database. These tests assert both halves of that.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict

import asyncpg
import pytest
from conftest import BIRD_DBS, DATA_DIR, connect_as_reader

# Every test here needs the live databases and the BIRD questions file, so the
# whole module is marked slow (deselect with -m 'not slow').
pytestmark = pytest.mark.slow

QUESTIONS_PATH = DATA_DIR / "mini_dev_postgresql.json"
SMOKE_TEST_COUNT = 25
SMOKE_TEST_SEED = 1234


def load_questions() -> list[dict]:
    if not QUESTIONS_PATH.is_file():
        pytest.fail(f"Questions file not found: {QUESTIONS_PATH}")
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))


async def test_every_db_id_has_its_own_database(reader_conn: asyncpg.Connection) -> None:
    questions = load_questions()
    db_ids = {q["db_id"] for q in questions}

    rows = await reader_conn.fetch("SELECT datname FROM pg_database")
    existing = {row["datname"] for row in rows}

    missing = sorted(db_ids - existing)
    assert not missing, f"These db_ids have no database: {missing}"
    assert db_ids == set(BIRD_DBS), (
        f"Question db_ids {sorted(db_ids)} do not match configured BIRD_DBS {BIRD_DBS}."
    )


async def test_sampled_gold_queries_run_against_own_database() -> None:
    questions = load_questions()
    sample = random.Random(SMOKE_TEST_SEED).sample(questions, SMOKE_TEST_COUNT)

    by_db: dict[str, list[dict]] = defaultdict(list)
    for q in sample:
        by_db[q["db_id"]].append(q)

    failures: list[str] = []
    for db_id, questions_for_db in by_db.items():
        conn = await connect_as_reader(db_id)
        try:
            # A bit more headroom than the role's 30s default for these references.
            await conn.execute("SET statement_timeout = '60s'")
            for q in questions_for_db:
                try:
                    await conn.fetch(q["SQL"])
                except asyncpg.PostgresError as exc:
                    failures.append(f"q{q['question_id']} ({db_id}): {exc}")
        finally:
            await conn.close()

    assert not failures, "Some gold queries failed:\n" + "\n".join(failures)
