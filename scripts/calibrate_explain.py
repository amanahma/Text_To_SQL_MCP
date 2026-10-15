"""Calibrate the executor's EXPLAIN cost limit from evidence, not guesswork.

For every one of the 500 BIRD gold queries we run ``check_sql`` (exactly as the
executor will) and then ``EXPLAIN (FORMAT JSON)`` on the query's *own* database,
as ``qg_reader`` inside a read-only transaction, and record the top plan node's
``Total Cost`` and ``Plan Rows``. We do the same for every case in
tests/safety_cases/resource_attacks.yaml. Both sets are written to
``logs/explain_calibration.csv``.

The script then prints the gold cost distribution (max / p99 / p95 / median,
overall and per db_id), proposes ``EXPLAIN_COST_LIMIT`` as 10x the gold maximum
rounded up, and reports how many resource attacks that limit would refuse. The
limit is derived from gold alone; the attack count is reported afterwards and
never used to move the limit.

Usage (PowerShell)::

    uv run python scripts/calibrate_explain.py

Needs the database up (``docker compose up -d``) and loaded.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlglot
import yaml

# This script lives in scripts/; put the project root on sys.path so the
# top-level `eval` package imports (queryguard is installed, eval is not).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from eval.harness import load_questions
from queryguard.db import allowed_databases, close_all, get_pool
from queryguard.safety import check_sql

RESOURCE_ATTACKS = PROJECT_ROOT / "tests" / "safety_cases" / "resource_attacks.yaml"
OUT_CSV = PROJECT_ROOT / "logs" / "explain_calibration.csv"
DEFAULT_ATTACK_DB = "california_schools"
MAX_ROWS = 100


@dataclass(frozen=True)
class PlanCost:
    total_cost: float
    plan_rows: float


async def explain_cost(db_id: str, safe_sql: str) -> PlanCost:
    """Return the top plan node's Total Cost / Plan Rows for ``safe_sql`` on ``db_id``.

    Runs as qg_reader inside a read-only transaction with a short timeout. EXPLAIN
    (without ANALYZE) only plans the query, so it never executes the expensive work.
    """
    pool = await get_pool(db_id)
    async with pool.acquire() as conn, conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '5s'")
        rows = await conn.fetch("EXPLAIN (FORMAT JSON) " + safe_sql)
    plan = rows[0][0]
    if isinstance(plan, str):  # asyncpg hands back the JSON column as text
        plan = json.loads(plan)
    top = plan[0]["Plan"]
    return PlanCost(float(top["Total Cost"]), float(top["Plan Rows"]))


async def _tables_by_database() -> dict[str, set[str]]:
    """Map each configured database to the set of base-table names it holds."""
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
    """The lower-cased base-table names a query reads (best-effort, via sqlglot)."""
    tree = sqlglot.parse_one(sql, read="postgres")
    return {t.name.lower() for t in tree.find_all(sqlglot.exp.Table) if t.name}


def _attack_database(sql: str, index: dict[str, set[str]]) -> str:
    """Pick the database an attack query belongs to: the one holding its tables.

    generate_series-only queries name no real table and run on the default db.
    """
    wanted = _referenced_tables(sql)
    if not wanted:
        return DEFAULT_ATTACK_DB
    for db_id, tables in index.items():
        if wanted <= tables:
            return db_id
    return DEFAULT_ATTACK_DB


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of ``values`` (pct in 0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _write_csv(rows: list[dict[str, object]]) -> None:
    """Write all calibration rows to the CSV (sync; called via asyncio.to_thread)."""
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["kind", "question_id", "db_id", "difficulty", "total_cost", "plan_rows"]
        )
        writer.writeheader()
        writer.writerows(rows)


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "n": len(values),
        "max": max(values) if values else 0.0,
        "p99": _percentile(values, 99),
        "p95": _percentile(values, 95),
        "median": _percentile(values, 50),
    }


async def calibrate() -> None:
    questions = load_questions()
    table_index = await _tables_by_database()

    gold_rows: list[dict[str, object]] = []
    gold_costs: list[float] = []
    per_db_costs: dict[str, list[float]] = {}
    blocked: list[tuple[int, str]] = []

    for question in questions:
        qid = question["question_id"]
        db_id = question["db_id"]
        difficulty = question["difficulty"]
        result = check_sql(question["SQL"], max_rows=MAX_ROWS + 1)
        if not result.ok or result.safe_sql is None:
            blocked.append((qid, result.errors[0] if result.errors else "(no message)"))
            continue
        cost = await explain_cost(db_id, result.safe_sql)
        gold_rows.append(
            {
                "kind": "gold",
                "question_id": qid,
                "db_id": db_id,
                "difficulty": difficulty,
                "total_cost": cost.total_cost,
                "plan_rows": cost.plan_rows,
            }
        )
        gold_costs.append(cost.total_cost)
        per_db_costs.setdefault(db_id, []).append(cost.total_cost)

    attack_rows: list[dict[str, object]] = []
    attack_costs: dict[str, float] = {}
    cases = yaml.safe_load(RESOURCE_ATTACKS.read_text(encoding="utf-8"))
    for case in cases:
        aid = case["id"]
        sql = case["sql"]
        db_id = _attack_database(sql, table_index)
        result = check_sql(sql, max_rows=MAX_ROWS + 1)
        if not result.ok or result.safe_sql is None:
            attack_rows.append(
                {
                    "kind": "attack",
                    "question_id": aid,
                    "db_id": db_id,
                    "difficulty": "blocked_by_check_sql",
                    "total_cost": "",
                    "plan_rows": "",
                }
            )
            continue
        cost = await explain_cost(db_id, result.safe_sql)
        attack_rows.append(
            {
                "kind": "attack",
                "question_id": aid,
                "db_id": db_id,
                "difficulty": "",
                "total_cost": cost.total_cost,
                "plan_rows": cost.plan_rows,
            }
        )
        attack_costs[aid] = cost.total_cost

    await asyncio.to_thread(_write_csv, gold_rows + attack_rows)

    # -------- report -------- #
    overall = _summary(gold_costs)
    proposed = math.ceil(10 * overall["max"])
    print(f"\nWrote {OUT_CSV.relative_to(PROJECT_ROOT)}")
    if blocked:
        print(f"WARNING: {len(blocked)} gold queries were blocked by check_sql: {blocked[:5]} ...")

    print("\n== Gold EXPLAIN Total Cost (overall) ==")
    print(
        f"  n={overall['n']}  max={overall['max']:.2f}  p99={overall['p99']:.2f}  "
        f"p95={overall['p95']:.2f}  median={overall['median']:.2f}"
    )

    print("\n== Gold EXPLAIN Total Cost (per db_id) ==")
    for db_id in sorted(per_db_costs):
        s = _summary(per_db_costs[db_id])
        print(
            f"  {db_id:<28} n={s['n']:>3}  max={s['max']:>12.2f}  p99={s['p99']:>12.2f}  "
            f"p95={s['p95']:>12.2f}  median={s['median']:>10.2f}"
        )

    print("\n== Proposed limit ==")
    print(f"  10 x gold max ({overall['max']:.2f}) = {10 * overall['max']:.2f}")
    print(f"  EXPLAIN_COST_LIMIT (rounded up) = {proposed}")

    refused = sorted(aid for aid, cost in attack_costs.items() if cost > proposed)
    passed = sorted(aid for aid, cost in attack_costs.items() if cost <= proposed)
    print("\n== Resource attacks vs proposed limit ==")
    print(f"  refused by cost gate ({len(refused)}/{len(attack_costs)}): {refused}")
    print(f"  under the limit (would rely on the 5s timeout): {passed}")
    for aid in sorted(attack_costs):
        print(f"    {aid:<36} cost={attack_costs[aid]:>16.2f}")


async def _amain() -> None:
    try:
        await calibrate()
    finally:
        await close_all()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
