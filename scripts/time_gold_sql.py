"""Time every BIRD gold query against its own database, as the read-only role.

Runs all 500 gold queries from data/mini_dev_postgresql.json as qg_reader, each
against its own db_id database, with statement_timeout = '120s'. Writes per-query
timings to logs/gold_timing.csv and prints a latency + failure summary.

Usage:
    uv run python scripts/time_gold_sql.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import time
from collections import defaultdict
from pathlib import Path

import asyncpg
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = PROJECT_ROOT / "data" / "mini_dev_postgresql.json"
CSV_PATH = PROJECT_ROOT / "logs" / "gold_timing.csv"
STATEMENT_TIMEOUT = "120s"

_ENV = dotenv_values(PROJECT_ROOT / ".env")


def connect_kwargs(database: str) -> dict[str, object]:
    return {
        "host": _ENV.get("DB_HOST", "localhost"),
        "port": int(_ENV.get("DB_PORT", "5433")),
        "user": _ENV.get("READER_USER", "qg_reader"),
        "password": _ENV.get("READER_PASSWORD"),
        "database": database,
    }


async def run_all(questions: list[dict]) -> list[dict]:
    """Execute every query against its own database; return a result row per query."""
    by_db: dict[str, list[dict]] = defaultdict(list)
    for question in questions:
        by_db[question["db_id"]].append(question)

    results: list[dict] = []
    for db_id in sorted(by_db):
        conn = await asyncpg.connect(**connect_kwargs(db_id))
        try:
            await conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            for question in by_db[db_id]:
                start = time.perf_counter()
                ok = True
                error = ""
                rows = 0
                try:
                    fetched = await conn.fetch(question["SQL"])
                    rows = len(fetched)
                except asyncpg.PostgresError as exc:
                    ok = False
                    error = f"{type(exc).__name__}: {exc}"
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                results.append(
                    {
                        "question_id": question["question_id"],
                        "db_id": db_id,
                        "difficulty": question.get("difficulty", ""),
                        "ok": ok,
                        "ms": round(elapsed_ms, 1),
                        "rows": rows,
                        "error": error,
                    }
                )
        finally:
            await conn.close()
    return results


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    rank = (len(sorted_values) - 1) * fraction
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_values[int(rank)]
    return sorted_values[low] * (high - rank) + sorted_values[high] * (rank - low)


def write_csv(results: list[dict]) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["question_id", "db_id", "difficulty", "ok", "ms", "rows"])
        for r in results:
            writer.writerow(
                [r["question_id"], r["db_id"], r["difficulty"], int(r["ok"]), r["ms"], r["rows"]]
            )


def report(results: list[dict]) -> None:
    failures = [r for r in results if not r["ok"]]
    ok_results = [r for r in results if r["ok"]]
    ok_ms = sorted(r["ms"] for r in ok_results)

    print(f"Ran {len(results)} gold queries ({len(ok_results)} ok, {len(failures)} failed).")
    print(f"Per-query timings written to {CSV_PATH}")

    print("\n== Failures ==")
    if failures:
        for r in failures:
            print(f"  q{r['question_id']} ({r['db_id']}, {r['ms']:.0f} ms): {r['error']}")
    else:
        print("  none")

    print("\n== Latency over successful queries (ms) ==")
    if ok_ms:
        print(f"  median : {percentile(ok_ms, 0.50):8.1f}")
        print(f"  p90    : {percentile(ok_ms, 0.90):8.1f}")
        print(f"  p95    : {percentile(ok_ms, 0.95):8.1f}")
        print(f"  p99    : {percentile(ok_ms, 0.99):8.1f}")
        print(f"  max    : {ok_ms[-1]:8.1f}")

    thresholds = [("> 1s", 1000), ("> 5s", 5000), ("> 30s", 30000)]
    print("\n== Queries exceeding thresholds (successful queries) ==")
    header = f"  {'db_id':24}" + "".join(f"{label:>8}" for label, _ in thresholds)
    print(header)
    for db_id in sorted({r["db_id"] for r in ok_results}):
        counts = [
            sum(1 for r in ok_results if r["db_id"] == db_id and r["ms"] > ms)
            for _, ms in thresholds
        ]
        print(f"  {db_id:24}" + "".join(f"{c:>8}" for c in counts))
    overall = [sum(1 for r in ok_results if r["ms"] > ms) for _, ms in thresholds]
    print(f"  {'OVERALL':24}" + "".join(f"{c:>8}" for c in overall))


def main() -> None:
    if not QUESTIONS_PATH.is_file():
        raise SystemExit(f"Questions file not found: {QUESTIONS_PATH}")
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    results = asyncio.run(run_all(questions))
    write_csv(results)
    report(results)


if __name__ == "__main__":
    main()
