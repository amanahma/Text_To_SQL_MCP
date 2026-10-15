"""Read-only debugging tool for eval run files.

Usage::

    uv run python -m eval.inspect <run file> <question_id>
    uv run python -m eval.inspect <run file> --all-wrong

For one question it prints the record (question, evidence, difficulty, result,
correct, tool-call count; every tool call; the gold SQL and the model's final
SQL), then re-runs both the gold and the final SQL on the question's database and
compares their shapes.

Both queries run as ``qg_reader`` with exactly the settings the scorer uses
(``eval.harness.execute_for_scoring``): a read-only transaction, a 60s statement
timeout, parallelism disabled (so results are reproducible), and no row cap. The
only addition here is capturing column names, which the scorer discards.

Makes **no** LLM API calls; it only reads the run file, the BIRD questions, and
the database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from eval.harness import SCORING_TIMEOUT_S, load_questions
from queryguard.db import close_all, get_pool

ROW_PREVIEW = 10
CELL_WIDTH = 80
RESULT_PREVIEW = 500


@dataclass
class InspectResult:
    """Outcome of re-running one query, including column names."""

    status: str  # "ok" | "error"
    columns: list[str] | None
    rows: list[tuple[Any, ...]] | None
    error: str | None


async def execute_for_inspection(
    db_id: str, sql: str, timeout_s: int = SCORING_TIMEOUT_S
) -> InspectResult:
    """Run SQL as qg_reader exactly as the scorer does, but also capture columns."""
    try:
        pool = await get_pool(db_id)
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_s)}s'")
            await conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            stmt = await conn.prepare(sql)
            columns = [attr.name for attr in stmt.get_attributes()]
            records = await stmt.fetch()
        rows = [tuple(record) for record in records]
        return InspectResult("ok", columns, rows, None)
    except (asyncpg.PostgresError, OSError) as exc:
        return InspectResult("error", None, None, str(exc))


# --------------------------------------------------------------------------- #
# Run-file reading and small formatting helpers
# --------------------------------------------------------------------------- #
def read_records(run_path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL run file into a list of records."""
    path = Path(run_path)
    if not path.exists():
        raise SystemExit(f"Run file not found: {path}")
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def column_count(result: InspectResult) -> int | None:
    """Number of columns, preferring the names, falling back to the row width."""
    if result.columns:
        return len(result.columns)
    if result.rows:
        return len(result.rows[0])
    return None


def _truncate(value: Any, width: int = CELL_WIDTH) -> str:
    text = str(value)
    return text if len(text) <= width else text[:width] + "…"


def _format_row(row: tuple[Any, ...]) -> str:
    return "(" + ", ".join(_truncate(cell) for cell in row) + ")"


def _print_result(label: str, result: InspectResult | None) -> None:
    print(f"--- {label} (re-run) ---")
    if result is None:
        print("  (not run — no SQL)")
        print()
        return
    if result.status != "ok":
        print(f"  ERROR: {result.error}")
        print()
        return
    if result.columns:
        print(f"  column names ({len(result.columns)}): {result.columns}")
    else:
        print("  column names: (not available)")
    print(f"  column count: {column_count(result)}")
    print(f"  row count: {len(result.rows)}")
    print(f"  first {ROW_PREVIEW} rows:")
    if not result.rows:
        print("    (no rows)")
    for row in result.rows[:ROW_PREVIEW]:
        print(f"    {_format_row(row)}")
    print()


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
async def inspect_question(run_path: str | Path, question_id: int) -> None:
    records = read_records(run_path)
    record = next((r for r in records if r["question_id"] == question_id), None)
    if record is None:
        raise SystemExit(f"question_id {question_id} not found in {run_path}")

    questions = {q["question_id"]: q for q in load_questions()}
    question = questions.get(question_id)
    db_id = record["db_id"]
    gold_sql = question["SQL"] if question else None
    final_sql = record.get("final_sql")

    bar = "=" * 72
    print(bar)
    print(f"Question {question_id}  (db: {db_id})")
    print(bar)
    print(f"Question:   {question['question'] if question else '(not found in dataset)'}")
    print(f"Evidence:   {question.get('evidence', '') if question else '(not found in dataset)'}")
    print(f"Difficulty: {record.get('difficulty')}")
    print(f"Result:     {record.get('result')}")
    print(f"Correct:    {record.get('correct')}")
    print(f"Tool calls: {record.get('tool_call_count')}")
    print()

    print("--- Tool calls ---")
    tool_calls = record.get("tool_calls") or []
    if not tool_calls:
        print("  (none)")
    for call in tool_calls:
        marker = " [ERROR]" if call.get("is_error") else ""
        print(f"[{call.get('step')}] {call.get('name')}{marker}")
        print(f"    args: {json.dumps(call.get('arguments'), default=str)}")
        print(f"    -> {_truncate(call.get('result', ''), RESULT_PREVIEW)}")
    print()

    print("--- Gold SQL ---")
    print(f"  {gold_sql if gold_sql else '(not found in dataset)'}")
    print()
    print("--- Final SQL (model) ---")
    print(f"  {final_sql if final_sql else '(none — no_sql)'}")
    print()

    failed_generation = record.get("failed_generation")
    if failed_generation:
        print(
            "--- Failed generation (final turn tool_use_failed; for inspection only, NOT scored) ---"
        )
        print(f"  {_truncate(failed_generation, RESULT_PREVIEW)}")
        print()

    gold = await execute_for_inspection(db_id, gold_sql) if gold_sql else None
    pred = await execute_for_inspection(db_id, final_sql) if final_sql else None
    _print_result("Gold result", gold)
    _print_result("Final result", pred)

    if gold and pred and gold.status == "ok" and pred.status == "ok":
        same_cols = "yes" if column_count(gold) == column_count(pred) else "no"
        same_rows = "yes" if len(gold.rows) == len(pred.rows) else "no"
    else:
        same_cols = same_rows = "n/a"
    print(f"same column count: {same_cols}; same row count: {same_rows}")


def _shape(result: InspectResult | None) -> str:
    if result is None:
        return "no-sql"
    if result.status != "ok":
        return "ERR"
    return f"{column_count(result)}col/{len(result.rows)}row"


async def all_wrong(run_path: str | Path) -> None:
    records = read_records(run_path)
    questions = {q["question_id"]: q for q in load_questions()}
    wrong = [r for r in records if r.get("correct") is False]
    if not wrong:
        print("No records with correct=False.")
        return
    print(f"{len(wrong)} record(s) with correct=False:")
    for record in sorted(wrong, key=lambda r: r["question_id"]):
        qid = record["question_id"]
        db_id = record["db_id"]
        gold_sql = questions.get(qid, {}).get("SQL")
        final_sql = record.get("final_sql")
        gold = await execute_for_inspection(db_id, gold_sql) if gold_sql else None
        pred = await execute_for_inspection(db_id, final_sql) if final_sql else None
        print(
            f"  q{qid:<6} [{record.get('difficulty', ''):<11}] "
            f"tool_calls={record.get('tool_call_count', 0):<3} "
            f"gold={_shape(gold):<14} pred={_shape(pred)}"
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect an eval run file (read-only).")
    parser.add_argument("run_file", help="Path to a JSONL run file.")
    parser.add_argument("question_id", type=int, nargs="?", help="Question to inspect.")
    parser.add_argument(
        "--all-wrong", action="store_true", help="Summarise every correct=False record."
    )
    return parser.parse_args(argv)


async def _amain(coro: Awaitable[None]) -> None:
    try:
        await coro
    finally:
        await close_all()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.all_wrong:
        asyncio.run(_amain(all_wrong(args.run_file)))
        return
    if args.question_id is None:
        raise SystemExit("A question_id is required unless --all-wrong is given.")
    asyncio.run(_amain(inspect_question(args.run_file, args.question_id)))


if __name__ == "__main__":
    main()
