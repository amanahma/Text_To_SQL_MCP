"""queryguard.executor.run_checked: the safe query runner (safety layers 3-5).

Pure-logic tests (value conversion, text truncation, PII detection, the blocked
path, concurrency, connection-failure mapping) need no database and are fast.
Tests that genuinely exercise PostgreSQL — truncation of real rows, the EXPLAIN
cost gate, the statement_timeout, undefined-name hints, value round-tripping and
the audit line — are marked ``slow`` (deselect with -m 'not slow').

No test weakens a safety guarantee: the timeout test reaches a real 5s cancel
through the ordinary run_checked path, using a recursive-CTE count that clears the
cost gate honestly (the planner under-costs it) and runs until the timeout. There
is no seam to switch the cost gate off.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import uuid
from decimal import Decimal

import pytest

from queryguard import executor
from queryguard.executor import ExecResult, run_checked
from queryguard.safety import check_sql

DB = "california_schools"


# AUDIT_LOG_PATH is redirected to a per-test temp file by an autouse fixture in
# tests/conftest.py, so the tests below never write to the real audit log.


# --------------------------------------------------------------------------- #
# Pure value conversion / truncation / PII (no database)
# --------------------------------------------------------------------------- #
def test_json_safe_scalar_conversions() -> None:
    assert executor._json_safe(Decimal("1.50")) == 1.5
    assert executor._json_safe(Decimal("NaN")) == "NaN"  # non-finite -> string
    assert executor._json_safe(Decimal(2)) == 2.0
    assert executor._json_safe(dt.date(2020, 1, 2)) == "2020-01-02"
    # A naive datetime is deliberate here: we are checking ISO conversion, not tz.
    naive = dt.datetime(2020, 1, 2, 3, 4, 5)  # noqa: DTZ001
    assert executor._json_safe(naive) == "2020-01-02T03:04:05"
    assert executor._json_safe(dt.time(3, 4, 5)) == "03:04:05"
    assert executor._json_safe(dt.timedelta(days=1, hours=2)) == "1 day, 2:00:00"
    uid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert executor._json_safe(uid) == "11111111-1111-1111-1111-111111111111"


def test_json_safe_binary_and_containers() -> None:
    assert executor._json_safe(b"\x00\x01\x02") == "<binary 3 bytes>"
    assert executor._json_safe(memoryview(b"abcd")) == "<binary 4 bytes>"
    assert executor._json_safe([Decimal(2), None, "x"]) == [2.0, None, "x"]
    assert executor._json_safe({"k": "v"}) == {"k": "v"}  # dicts pass through


def test_truncate_text_at_boundary() -> None:
    assert executor._truncate_text("a" * 200) == "a" * 200  # exactly 200: untouched
    cut = executor._truncate_text("a" * 201)
    assert cut == "a" * 200 + "…[truncated]"


def test_pii_column_detection_strong_and_weak_tokens() -> None:
    # Strong tokens match as substrings (so mashed-together names are caught).
    for name in [
        "email",
        "customer_email",
        "admemail1",  # the real BIRD gap this rule closes
        "phone",
        "phone_number",
        "password",
        "aadhaar",
        "ssn",
        "user_ssn",
        # Weak tokens match only as whole words.
        "pan",
        "pan_card",
        "mobile",
        "api_token",
        "secret",
        "client_secret",
    ]:
        assert executor._is_pii_column(name), name
    for name in ["company", "pantry", "name", "city", "description", "panel", "tokens_used"]:
        assert not executor._is_pii_column(name), name


# --------------------------------------------------------------------------- #
# Error-text redaction (no database)
# --------------------------------------------------------------------------- #
def test_redact_db_error_class22_drops_quoted_value() -> None:
    # SQLSTATE 22P02 (invalid_text_representation) is class 22: keep text before the
    # first quote, so the offending value (here a real email) is never echoed.
    msg = 'invalid input syntax for type integer: "lkmonroe@acoe.org"'
    out = executor._redact_db_error(msg, "22P02")
    assert out == "invalid input syntax for type integer"
    assert "lkmonroe@acoe.org" not in out
    assert "@" not in out


def test_redact_db_error_numeric_range_and_date() -> None:
    assert (
        executor._redact_db_error('value "99999999999" is out of range for type integer', "22003")
        == "value"
    )
    assert (
        executor._redact_db_error('date/time field value out of range: "2020-13-40"', "22008")
        == "date/time field value out of range"
    )


def test_redact_db_error_masks_email_outside_class22() -> None:
    # Not a class-22 error, so no truncation, but an email-looking token is still
    # masked by the backstop.
    out = executor._redact_db_error("some detail about bob@example.com here", "XX000")
    assert "bob@example.com" not in out
    assert executor.MASK in out


def test_redact_db_error_leaves_plain_message_untouched() -> None:
    assert (
        executor._redact_db_error("relation does not exist", "42P01") == "relation does not exist"
    )


# --------------------------------------------------------------------------- #
# Blocked SQL never touches the database
# --------------------------------------------------------------------------- #
async def test_blocked_sql_is_blocked_and_db_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    async def spy_get_pool(_db: str):
        raise AssertionError("the database must not be touched when check_sql blocks the SQL")

    monkeypatch.setattr(executor, "get_pool", spy_get_pool)
    result = await run_checked(DB, "DROP TABLE schools")
    assert result.ok is False
    assert result.error_kind == "blocked"
    assert result.error  # carries check_sql's reason
    assert result.rows == [] and result.row_count == 0


async def test_blocked_audit_line_has_null_safe_sql(tmp_path) -> None:
    await run_checked(DB, "DELETE FROM schools")
    line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(line)
    assert record["status"] == "blocked"
    assert record["safe_sql"] is None


# --------------------------------------------------------------------------- #
# Concurrency: per-user semaphore
# --------------------------------------------------------------------------- #
async def test_same_user_runs_at_most_three_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"current": 0, "max": 0}

    async def fake_execute(*_args, **_kwargs) -> ExecResult:
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.3)
        state["current"] -= 1
        return ExecResult(True, [], [], 0, False, 0.0)

    monkeypatch.setattr(executor, "_execute", fake_execute)
    await asyncio.gather(*(run_checked(DB, "SELECT 1", user_id="same") for _ in range(7)))
    assert state["max"] == 3  # the semaphore caps concurrency at exactly 3


async def test_different_users_run_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_execute(*_args, **_kwargs) -> ExecResult:
        await asyncio.sleep(0.5)
        return ExecResult(True, [], [], 0, False, 0.0)

    monkeypatch.setattr(executor, "_execute", fake_execute)
    start = time.perf_counter()
    await asyncio.gather(*(run_checked(DB, "SELECT 1", user_id=f"u{i}") for i in range(5)))
    elapsed = time.perf_counter() - start
    # Five independent users => all five ~0.5s calls overlap; serialized would be 2.5s.
    assert elapsed < 1.5


# --------------------------------------------------------------------------- #
# Connection failures map to "unavailable"
# --------------------------------------------------------------------------- #
async def test_connection_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(_db: str):
        raise ConnectionRefusedError("[WinError 1225] refused the network connection")

    monkeypatch.setattr(executor, "get_pool", boom)
    result = await run_checked(DB, "SELECT 1")
    assert result.error_kind == "unavailable"
    assert result.error.startswith("database unreachable:")
    assert "\n" not in result.error  # single line, no stack trace


# --------------------------------------------------------------------------- #
# Database-backed behaviour
# --------------------------------------------------------------------------- #
_CONVERSIONS_SQL = (
    "SELECT 1.5 AS d, 'NaN'::numeric AS nan_d, DATE '2020-01-02' AS dte, "
    "TIMESTAMP '2020-01-02 03:04:05' AS ts, TIME '03:04:05' AS tm, "
    "INTERVAL '1 day 2 hours' AS iv, "
    "'11111111-1111-1111-1111-111111111111'::uuid AS uid, "
    "'\\xdeadbeef'::bytea AS bin, ARRAY[1, 2, 3] AS arr, "
    "'" + "y" * 300 + "' AS longtext"
)


@pytest.mark.slow
async def test_only_safe_sql_is_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_checked passes check_sql's safe_sql to execution, never the caller text."""
    seen: dict[str, str] = {}

    async def spy_cost(_conn, safe_sql: str) -> float:
        seen["cost"] = safe_sql
        return 0.0

    async def spy_fetch(_conn, safe_sql: str, _max_rows: int):
        seen["exec"] = safe_sql
        return (["n"], [], False)

    monkeypatch.setattr(executor, "_estimate_cost", spy_cost)
    monkeypatch.setattr(executor, "_fetch_rows", spy_fetch)

    raw = "SELECT cdscode FROM schools LIMIT 999999"
    expected = check_sql(raw, max_rows=101).safe_sql
    await run_checked(DB, raw)
    assert seen["exec"] == expected
    assert seen["cost"] == expected
    assert seen["exec"] != raw  # the oversized LIMIT was rewritten by R7


@pytest.mark.slow
async def test_truncation_over_cap() -> None:
    result = await run_checked(DB, "SELECT cdscode FROM schools")  # ~17k rows
    assert result.ok is True
    assert result.truncated is True
    assert result.row_count == 100
    assert len(result.rows) == 100
    assert result.warnings == ["results are capped at 100 rows"]


@pytest.mark.slow
async def test_exactly_cap_is_not_truncated() -> None:
    result = await run_checked(DB, "SELECT cdscode FROM schools LIMIT 100")
    assert result.ok is True
    assert result.truncated is False
    assert result.row_count == 100
    assert result.warnings == []  # the caller's own LIMIT was within the cap


@pytest.mark.slow
async def test_cost_gate_refuses_without_executing(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = {"n": 0}
    original = executor._fetch_rows

    async def counting_fetch(*args, **kwargs):
        executed["n"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(executor, "_fetch_rows", counting_fetch)
    # A resource_attacks.yaml cross join whose planner estimate is far over the limit.
    attack = (
        "SELECT count(*) FROM generate_series(1, 100000) AS g1 "
        "CROSS JOIN generate_series(1, 100000) AS g2"
    )
    result = await run_checked(DB, attack)
    assert result.error_kind == "cost"
    assert executed["n"] == 0  # the query was never executed
    assert "narrow the query" in result.error


@pytest.mark.slow
async def test_statement_timeout_maps_to_timeout() -> None:
    # An unbounded recursive CTE counted with COUNT(*). The planner badly
    # under-costs recursive CTEs, so this clears the cost gate (estimate ~5), and
    # COUNT forces the recursion to run forever — only the 5s statement_timeout
    # stops it. This goes through the real run_checked path: there is no seam to
    # skip the cost gate, because this query genuinely passes it.
    recursion = (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r"
    )
    start = time.perf_counter()
    result = await run_checked(DB, recursion)
    elapsed = time.perf_counter() - start
    assert result.error_kind == "timeout"
    assert "5 seconds" in result.error
    # The 5s server-side timeout genuinely elapsed (not an instant failure) and the
    # query did not hang. The upper bound is loose: cold pool connect and the
    # recursion rollback add wall-clock overhead on top of the 5s cancel.
    assert 4.0 < elapsed < 15.0


@pytest.mark.slow
async def test_undefined_column_suggests_closest_names() -> None:
    result = await run_checked(DB, "SELECT cityy FROM schools")
    assert result.error_kind == "sql_error"
    assert "Did you mean" in result.error
    assert "city" in result.error
    assert "list_tables" in result.error


@pytest.mark.slow
async def test_insufficient_privilege_message(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    async def raise_priv(*_args, **_kwargs):
        raise asyncpg.InsufficientPrivilegeError("permission denied for function pg_read_file")

    monkeypatch.setattr(executor, "_fetch_rows", raise_priv)
    result = await run_checked(DB, "SELECT 1")
    assert result.error_kind == "sql_error"
    assert result.error == "this function or table is not allowed."


@pytest.mark.slow
async def test_value_conversions_round_trip() -> None:
    result = await run_checked(DB, _CONVERSIONS_SQL)
    assert result.ok is True
    row = dict(zip(result.columns, result.rows[0], strict=True))
    assert row["d"] == 1.5
    assert row["nan_d"] == "NaN"
    assert row["dte"] == "2020-01-02"
    assert row["ts"] == "2020-01-02T03:04:05"
    assert row["tm"] == "03:04:05"
    assert row["iv"] == "1 day, 2:00:00"
    assert row["uid"] == "11111111-1111-1111-1111-111111111111"
    assert row["bin"] == "<binary 4 bytes>"  # \xdeadbeef = 4 bytes
    assert row["arr"] == [1, 2, 3]
    assert row["longtext"] == "y" * 200 + "…[truncated]"


@pytest.mark.slow
async def test_pii_masking_integration() -> None:
    result = await run_checked(
        DB,
        "SELECT 'a' AS email, 'b' AS company, 'c' AS phone_number, 'd' AS pantry",
    )
    row = dict(zip(result.columns, result.rows[0], strict=True))
    assert row["email"] == "***"
    assert row["phone_number"] == "***"
    assert row["company"] == "b"  # not masked
    assert row["pantry"] == "d"  # not masked


@pytest.mark.slow
async def test_value_based_email_masking_in_plain_column() -> None:
    # An email-looking value is masked even when the column name is innocuous.
    result = await run_checked(DB, "SELECT 'a@b.com' AS notes, 'plain text' AS label")
    row = dict(zip(result.columns, result.rows[0], strict=True))
    assert row["notes"] == "***"
    assert row["label"] == "plain text"


@pytest.mark.slow
async def test_cast_error_does_not_leak_admemail_value() -> None:
    # Casting a real email column to integer errors with the value quoted in the
    # message; redaction must drop it so no real email reaches the client.
    result = await run_checked(
        "california_schools", "SELECT CAST(admemail1 AS integer) FROM schools"
    )
    assert result.error_kind == "sql_error"
    assert result.error.startswith("invalid input syntax for type integer")
    assert "@" not in result.error  # no email leaked
    assert '"' not in result.error  # no quoted value at all


@pytest.mark.slow
async def test_cast_error_does_not_leak_member_email_value() -> None:
    result = await run_checked("student_club", "SELECT CAST(email AS integer) FROM member")
    assert result.error_kind == "sql_error"
    assert result.error.startswith("invalid input syntax for type integer")
    assert "@" not in result.error


@pytest.mark.slow
async def test_date_cast_error_is_redacted() -> None:
    result = await run_checked("california_schools", "SELECT CAST('2020-13-40' AS date)")
    assert result.error_kind == "sql_error"
    assert "2020-13-40" not in result.error  # the offending value is gone
    assert '"' not in result.error


@pytest.mark.slow
async def test_numeric_out_of_range_error_is_redacted() -> None:
    result = await run_checked(
        "california_schools", "SELECT CAST('99999999999999999999' AS integer)"
    )
    assert result.error_kind == "sql_error"
    assert "99999999999999999999" not in result.error
    assert '"' not in result.error


@pytest.mark.slow
async def test_audit_one_line_per_call_without_secrets(tmp_path) -> None:
    await run_checked(DB, "SELECT 1")
    await run_checked(DB, "DROP TABLE schools")  # blocked
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 2
    ok, blocked = (json.loads(line) for line in lines)

    assert set(ok) == {
        "ts",
        "user_id",
        "database",
        "safe_sql",
        "status",
        "elapsed_ms",
        "row_count",
        "truncated",
    }
    assert ok["status"] == "ok"
    assert ok["database"] == DB
    assert ok["safe_sql"] is not None
    assert ok["user_id"] == "local"
    assert blocked["status"] == "blocked"
    assert blocked["safe_sql"] is None

    # No credentials or connection string ever reach the audit log.
    low = text.lower()
    assert "password" not in low
    assert "postgres://" not in low
    assert "@localhost" not in low
    assert "qg_reader" not in low


async def test_audit_never_touches_the_real_log(tmp_path) -> None:
    """The conftest autouse fixture isolates the audit log from the real one.

    Proves that a run_checked call writes its audit line to this test's temp file,
    not to the project's logs/audit.jsonl. Uses the blocked path, so no database is
    needed (this test stays fast).
    """
    # The autouse fixture in conftest.py redirected AUDIT_LOG_PATH into tmp_path.
    assert executor._audit_path() == tmp_path / "audit.jsonl"
    real_log = executor.PROJECT_ROOT / "logs" / "audit.jsonl"
    assert executor._audit_path() != real_log

    before = real_log.read_text(encoding="utf-8") if real_log.exists() else None
    await run_checked(DB, "DROP TABLE schools")  # blocked -> one audit line, no DB
    # The line landed in the temp file...
    assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    # ...and the real audit log is byte-for-byte unchanged.
    after = real_log.read_text(encoding="utf-8") if real_log.exists() else None
    assert after == before
