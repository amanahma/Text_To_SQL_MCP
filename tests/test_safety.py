"""Tests for the SQL safety layer (no database).

The attack and valid-query cases live as data in ``tests/safety_cases/*.yaml`` so
the owner can read, audit and extend the corpus without touching Python. Until
``check_sql`` is implemented every case that calls it fails with
NotImplementedError; the loader, the CheckResult and the coverage tests pass.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import sqlglot
import yaml
from sqlglot import exp

from queryguard.safety import MAX_SQL_CHARS, CheckResult, check_sql

CASES_DIR = Path(__file__).parent / "safety_cases"
MAX_ROWS = 100


def _load(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load((CASES_DIR / name).read_text(encoding="utf-8"))


ATTACKS = _load("attacks.yaml")
VALID = _load("valid_queries.yaml")


def _attack_sql(case: dict[str, Any]) -> str:
    """The SQL for an attack case, building the oversize string on demand."""
    if case.get("oversize"):
        return "SELECT '" + "a" * (MAX_SQL_CHARS + 1) + "'"
    return case["sql"]


def _by_id(cases: list[dict[str, Any]], case_id: str) -> dict[str, Any]:
    return next(c for c in cases if c["id"] == case_id)


def _outermost_limit(sql: str, dialect: str = "postgres") -> int | None:
    """The integer LIMIT on the outermost query of ``sql`` (None if there is none).

    Unwraps a parenthesised/subquery root and understands both LIMIT and
    FETCH FIRST n ROWS ONLY. Assumes a literal limit, which is what safe_sql
    always carries after R7.
    """
    expr = sqlglot.parse_one(sql, read=dialect)
    while isinstance(expr, (exp.Subquery, exp.Paren)):
        expr = expr.this
    node = expr.args.get("limit")
    if node is None:
        return None
    if isinstance(node, exp.Fetch):
        count = node.args.get("count")
        return int(count.name) if count is not None else None
    return int(node.expression.name)


def _reparses_to_one_statement(sql: str) -> bool:
    statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    return len(statements) == 1


# --- every attack must be blocked, by a known rule -------------------------- #
@pytest.mark.parametrize("case", ATTACKS, ids=[c["id"] for c in ATTACKS])
def test_attack_is_blocked(case: dict[str, Any]) -> None:
    result = check_sql(_attack_sql(case))
    assert result.ok is False
    assert result.safe_sql is None
    assert result.errors, "a blocked query must carry at least one error message"
    # The first error must name the rule that blocked it. An earlier rule may
    # legitimately fire first (e.g. sqlglot cannot parse the statement, so R2
    # blocks it); such cases list the acceptable rule(s) in `also_ok_rules`.
    allowed = [case["rule"], *case.get("also_ok_rules", [])]
    assert any(result.errors[0].startswith(f"{rule}:") for rule in allowed), (
        f"{case['id']}: first error {result.errors[0]!r} does not start with "
        f"any of {[f'{r}:' for r in allowed]}"
    )


# --- every valid query must pass unchanged in meaning ----------------------- #
@pytest.mark.parametrize("case", VALID, ids=[c["id"] for c in VALID])
def test_valid_query_passes(case: dict[str, Any]) -> None:
    result = check_sql(case["sql"])
    assert result.ok is True, f"unexpectedly blocked {case['id']}: {result.errors}"
    assert result.errors == []
    assert result.safe_sql is not None
    assert _reparses_to_one_statement(result.safe_sql)
    limit = _outermost_limit(result.safe_sql)
    assert limit is not None, "R7 must leave an outermost LIMIT"
    assert limit <= MAX_ROWS
    assert limit == case["expect_limit"]

    # R7 warns iff it changed the outermost LIMIT. The expectation is stated
    # explicitly per case (expect_warning); the test never re-derives it.
    if case["expect_warning"]:
        assert result.warnings, f"{case['id']}: a changed LIMIT must produce a warning"
        assert all(w.startswith("R7:") for w in result.warnings)
    else:
        assert result.warnings == []


@pytest.mark.parametrize("case", VALID, ids=[c["id"] for c in VALID])
def test_valid_query_is_idempotent(case: dict[str, Any]) -> None:
    first = check_sql(case["sql"])
    assert first.ok and first.safe_sql is not None
    second = check_sql(first.safe_sql)
    assert second.ok
    assert second.safe_sql == first.safe_sql


# --- comments from the input never survive into safe_sql -------------------- #
def test_comments_never_appear_in_safe_sql() -> None:
    for case_id, token in (
        ("v_line_comment", "pick the school name"),
        ("v_block_comment", "just the name"),
    ):
        case = _by_id(VALID, case_id)
        result = check_sql(case["sql"])
        assert result.ok and result.safe_sql is not None
        assert token not in result.safe_sql
        assert "--" not in result.safe_sql
        assert "/*" not in result.safe_sql


# --- a different max_rows is honoured ---------------------------------------- #
def test_max_rows_is_respected() -> None:
    no_limit = check_sql("SELECT School FROM schools", max_rows=5)
    assert no_limit.ok and _outermost_limit(no_limit.safe_sql) == 5

    too_big = check_sql("SELECT School FROM schools LIMIT 1000", max_rows=5)
    assert too_big.ok and _outermost_limit(too_big.safe_sql) == 5

    under = check_sql("SELECT School FROM schools LIMIT 3", max_rows=5)
    assert under.ok and _outermost_limit(under.safe_sql) == 3


# --- CheckResult is an immutable value object ------------------------------- #
def test_check_result_is_frozen() -> None:
    result = CheckResult(ok=True, safe_sql="SELECT 1", errors=[], warnings=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False  # type: ignore[misc]


# --- coverage: which rule each attack targets ------------------------------- #
def test_zzz_attack_coverage_by_rule() -> None:
    """Print the attack count per rule and assert R1-R6 are all covered.

    Named to sort last so its output trails the file. Run ``pytest -s`` to see
    the printed breakdown.
    """
    counts = Counter(c["rule"] for c in ATTACKS)
    print("\nAttack cases by rule: " + ", ".join(f"{r}={counts[r]}" for r in sorted(counts)))
    print(f"Total attacks: {len(ATTACKS)}")
    for rule in ("R1", "R2", "R3", "R4", "R5", "R6"):
        assert counts[rule] > 0, f"no attack cases target {rule}"
    assert len(ATTACKS) >= 80
