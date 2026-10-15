"""The 500 BIRD gold queries must survive the safety layer unchanged in meaning.

Two tests:

* ``test_gold_queries_all_pass`` (no database): every gold SQL must pass
  ``check_sql``. A gold query that the safety layer blocks is a false positive we
  want to see by question_id.
* ``test_gold_safe_sql_matches_original`` (``slow``, needs the database): for every
  gold query whose *uncapped* result fits within ``max_rows`` rows, the re-rendered
  ``safe_sql`` must return the same result set as the original. This proves R8's
  re-rendering never changes what a query means. Queries whose result exceeds the
  cap are skipped, because the row cap legitimately changes their output.
"""

from __future__ import annotations

import pytest

from eval.harness import execute_for_scoring, load_questions
from eval.scoring import is_correct
from queryguard.safety import check_sql

# Both tests need the BIRD questions file (load_questions reads
# data/mini_dev_postgresql.json) and the second also needs the live database, so
# the whole module is marked slow (deselect with -m 'not slow').
pytestmark = pytest.mark.slow

MAX_ROWS = 100


def test_gold_queries_all_pass() -> None:
    """No database: every gold query must be accepted by check_sql."""
    blocked: list[tuple[int, str]] = []
    for question in load_questions():
        result = check_sql(question["SQL"])
        if not result.ok:
            first_error = result.errors[0] if result.errors else "(no error message)"
            blocked.append((question["question_id"], first_error))
    assert not blocked, "check_sql falsely blocked gold queries:\n" + "\n".join(
        f"  question {qid}: {err}" for qid, err in blocked
    )


@pytest.mark.slow
async def test_gold_safe_sql_matches_original() -> None:
    """Database: safe_sql returns the same rows as the original (for small results)."""
    mismatches: list[int] = []
    skipped_too_large = 0
    for question in load_questions():
        qid = question["question_id"]
        db_id = question["db_id"]
        gold_sql = question["SQL"]

        result = check_sql(gold_sql)
        assert result.ok and result.safe_sql is not None, (
            f"gold query {qid} was blocked: {result.errors}"
        )

        original = await execute_for_scoring(db_id, gold_sql)
        assert original.status == "ok", f"gold query {qid} failed to run: {original.error}"
        if original.row_count is None or original.row_count > MAX_ROWS:
            skipped_too_large += 1
            continue

        safe = await execute_for_scoring(db_id, result.safe_sql)
        assert safe.status == "ok", f"safe_sql for question {qid} failed to run: {safe.error}"
        if not is_correct(safe.rows, original.rows):
            mismatches.append(qid)

    assert not mismatches, (
        f"safe_sql changed the result set for question(s): {mismatches} "
        f"({skipped_too_large} skipped as larger than {MAX_ROWS} rows)"
    )
