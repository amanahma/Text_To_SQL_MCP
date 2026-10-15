"""Phase 2B red-team findings, pinned as FIXED in Phase 2C.

Each case in ``tests/safety_cases/redteam.yaml`` was a gap found by attacking
``check_sql`` and executing the result against the live database. Phase 2C closed
them, so these tests now assert the *fixed* behaviour (no longer xfail):

- ``block_r6``       state-changing functions (pg_logical_emit_message,
  txid_current, pg_current_xact_id, setseed) are rejected by the R6 allow-list.
- ``cap_r7``         FETCH ... WITH TIES is converted to a plain LIMIT, so it can
  no longer return more than max_rows rows.
- ``reject_r4_hint`` VALUES and TABLE stay rejected (owner decision), but the R4
  error now suggests the SELECT form.
- ``reject_r2``      ``SELECT 1 LIMIT ALL`` (no FROM) stays rejected at R2.

The database side of the fix (EXECUTE revoked on the block_r6 functions) is
checked in ``tests/test_reader_revokes.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from queryguard.safety import check_sql

CASES_DIR = Path(__file__).parent / "safety_cases"
REDTEAM = yaml.safe_load((CASES_DIR / "redteam.yaml").read_text(encoding="utf-8"))


def _of_kind(kind: str) -> list[Any]:
    return [pytest.param(c, id=c["id"]) for c in REDTEAM if c["kind"] == kind]


@pytest.mark.parametrize("case", _of_kind("block_r6"))
def test_state_changing_function_is_blocked(case: dict[str, Any]) -> None:
    result = check_sql(case["sql"])
    assert result.ok is False, f"{case['id']}: should be blocked ({case['why']})"
    assert result.safe_sql is None
    assert result.errors[0].startswith("R6:")


@pytest.mark.parametrize("case", _of_kind("cap_r7"))
def test_with_ties_is_capped(case: dict[str, Any]) -> None:
    result = check_sql(case["sql"])
    assert result.ok is True, f"{case['id']}: {result.errors}"
    assert result.safe_sql is not None
    # The row cap now holds: WITH TIES is gone, replaced by a plain LIMIT.
    assert "WITH TIES" not in result.safe_sql.upper()
    assert any("WITH TIES removed" in w for w in result.warnings), result.warnings


@pytest.mark.parametrize("case", _of_kind("reject_r4_hint"))
def test_values_table_rejected_with_select_hint(case: dict[str, Any]) -> None:
    result = check_sql(case["sql"])
    assert result.ok is False
    assert result.safe_sql is None
    assert result.errors[0].startswith("R4:")
    # The hint must point the AI at the SELECT form.
    assert "SELECT" in result.errors[0], result.errors[0]


@pytest.mark.parametrize("case", _of_kind("reject_r2"))
def test_limit_all_without_from_rejected(case: dict[str, Any]) -> None:
    result = check_sql(case["sql"])
    assert result.ok is False
    assert result.safe_sql is None
    assert result.errors[0].startswith("R2:")
