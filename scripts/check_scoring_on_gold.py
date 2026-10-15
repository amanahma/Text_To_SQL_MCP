"""Sanity-check eval.scoring.is_correct against the 500 BIRD gold queries.

This does not touch eval/scoring.py; it only *calls* is_correct. All SQL runs
through the harness's own executor (``execute_for_scoring``): as qg_reader, each
question against its own database, no row cap, 60s timeout.

Three checks:

1. Print the Python type of one result row handed to is_correct.
2. Positive: run every gold query twice and assert is_correct(run1, run2) is True
   (a query must score equal to itself). Report True/False/exception counts and
   name every question that is not True.
3. Negative: compare each question's gold result with another question's gold
   result from the same database. Very few should come back True; list them.

Run:
    uv run python scripts/check_scoring_on_gold.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

# This script lives in scripts/; put the project root on sys.path so the
# top-level `eval` package imports (queryguard is installed, eval is not).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from eval.harness import ExecResult, execute_for_scoring, load_questions
from eval.scoring import is_correct
from queryguard.db import close_all

CONCURRENCY = 4


async def _execute_resilient(db_id: str, sql: str, attempts: int = 3) -> ExecResult:
    """execute_for_scoring, retried on client-side connection drops.

    execute_for_scoring already turns server (PostgresError) and OS errors into an
    error ExecResult; this only guards against a stale pooled connection being
    handed out (asyncpg InterfaceError), by retrying with a fresh acquire.
    """
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await execute_for_scoring(db_id, sql)
        except Exception as exc:  # noqa: BLE001 - keep the sweep going, record the error
            last = exc
            await asyncio.sleep(0.2)
    return ExecResult("error", None, None, f"{type(last).__name__}: {last}")


async def run_gold_twice(
    questions: list[dict],
) -> tuple[dict[int, ExecResult], dict[int, ExecResult]]:
    """Execute every gold query twice (back to back) -> (run1, run2) by question_id."""
    semaphore = asyncio.Semaphore(CONCURRENCY)
    run1: dict[int, ExecResult] = {}
    run2: dict[int, ExecResult] = {}
    done = 0
    total = len(questions)

    async def one(question: dict) -> None:
        nonlocal done
        qid, db_id, sql = question["question_id"], question["db_id"], question["SQL"]
        async with semaphore:
            run1[qid] = await _execute_resilient(db_id, sql)
            run2[qid] = await _execute_resilient(db_id, sql)
        done += 1
        if done % 50 == 0 or done == total:
            print(f"  ... {done}/{total} questions executed", file=sys.stderr, flush=True)

    await asyncio.gather(*(one(q) for q in questions))
    return run1, run2


def print_example_row_type(questions: list[dict], results: dict[int, ExecResult]) -> None:
    print("=" * 70)
    print("1. Type of the result rows passed to is_correct")
    print("=" * 70)
    for question in questions:
        res = results[question["question_id"]]
        if res.status == "ok" and res.rows:
            row = res.rows[0]
            print(f"example question_id : {question['question_id']} ({question['db_id']})")
            print(f"type(rows)          : {type(res.rows).__name__}")
            print(f"type(rows[0])       : {type(row).__name__}")
            print(f"rows[0]             : {row!r}")
            print(f"per-column types    : {[type(v).__name__ for v in row]}")
            return
    print("No question returned a non-empty row set.")


def positive_check(questions: list[dict], run1: dict, run2: dict) -> None:
    print()
    print("=" * 70)
    print("2. Positive check: is_correct(gold_run1, gold_run2) should be True")
    print("=" * 70)
    true_count = false_count = exception_count = exec_error_count = 0
    not_true: list[tuple[int, str]] = []
    exceptions: list[tuple[int, str]] = []
    exec_errors: list[int] = []

    for question in questions:
        qid = question["question_id"]
        r1, r2 = run1[qid], run2[qid]
        if r1.status != "ok" or r2.status != "ok":
            exec_error_count += 1
            exec_errors.append(qid)
            continue
        try:
            verdict = is_correct(r1.rows, r2.rows)
        except Exception as exc:  # noqa: BLE001 - we are probing for scorer crashes
            exception_count += 1
            exceptions.append((qid, f"{type(exc).__name__}: {exc}"))
            continue
        if verdict is True:
            true_count += 1
        else:
            false_count += 1
            not_true.append((qid, repr(verdict)))

    print(f"questions           : {len(questions)}")
    print(f"True                : {true_count}")
    print(f"False               : {false_count}")
    print(f"is_correct raised   : {exception_count}")
    print(f"gold failed to run  : {exec_error_count}")
    if not_true:
        print(f"False question_ids  : {[qid for qid, _ in not_true]}")
    if exceptions:
        print("exception details   :")
        for qid, msg in exceptions:
            print(f"  {qid}: {msg}")
    if exec_errors:
        print(f"gold-error ids      : {exec_errors}")


def negative_check(questions: list[dict], run1: dict) -> None:
    print()
    print("=" * 70)
    print("3. Negative check: a question's gold vs. another question's gold")
    print("   (same database). True here means two different questions produced")
    print("   indistinguishable result sets — should be rare.")
    print("=" * 70)
    by_db: dict[str, list[int]] = defaultdict(list)
    for question in questions:
        by_db[question["db_id"]].append(question["question_id"])

    compared = 0
    collisions: list[tuple[int, int]] = []
    exceptions: list[tuple[int, int, str]] = []

    for ids in by_db.values():
        ids = sorted(ids)
        if len(ids) < 2:
            continue  # cannot pair within a single-question database
        for i, qid in enumerate(ids):
            other = ids[(i + 1) % len(ids)]  # next question in the same database
            r1, r2 = run1[qid], run1[other]
            if r1.status != "ok" or r2.status != "ok":
                continue
            compared += 1
            try:
                verdict = is_correct(r1.rows, r2.rows)
            except Exception as exc:  # noqa: BLE001 - probing for scorer crashes
                exceptions.append((qid, other, f"{type(exc).__name__}: {exc}"))
                continue
            if verdict is True:
                collisions.append((qid, other))

    print(f"pairs compared      : {compared}")
    print(f"came back True      : {len(collisions)}")
    if collisions:
        print("True pairs (q vs other):")
        for qid, other in collisions:
            print(f"  {qid} == {other}")
    if exceptions:
        print(f"is_correct raised   : {len(exceptions)}")
        for qid, other, msg in exceptions:
            print(f"  {qid} vs {other}: {msg}")


async def main() -> None:
    questions = load_questions()
    print(f"Loaded {len(questions)} questions. Running each gold query twice...\n")
    try:
        run1, run2 = await run_gold_twice(questions)
    finally:
        await close_all()

    print_example_row_type(questions, run1)
    positive_check(questions, run1, run2)
    negative_check(questions, run1)


if __name__ == "__main__":
    asyncio.run(main())
