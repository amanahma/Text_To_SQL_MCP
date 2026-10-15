"""eval.inspect: the read-only run-file debugger (no DB, no LLM here).

SQL execution (``execute_for_inspection``) and the dataset lookup
(``load_questions``) are monkeypatched, so these tests exercise only the tool's
reading, formatting, and comparison logic.
"""

from __future__ import annotations

import json

import pytest

import eval.inspect as qi
from eval.inspect import InspectResult, column_count, read_records


def _ok(columns, rows) -> InspectResult:
    return InspectResult("ok", columns, rows, None)


def _err(message: str = "boom") -> InspectResult:
    return InspectResult("error", None, None, message)


def test_read_records_skips_blank_lines(tmp_path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps({"question_id": 1}) + "\n\n" + json.dumps({"question_id": 2}) + "\n",
        encoding="utf-8",
    )
    records = read_records(path)
    assert [r["question_id"] for r in records] == [1, 2]


def test_read_records_missing_file(tmp_path) -> None:
    with pytest.raises(SystemExit):
        read_records(tmp_path / "nope.jsonl")


def test_column_count_prefers_names_then_row_width() -> None:
    assert column_count(_ok(["a", "b"], [])) == 2  # from names
    assert column_count(_ok([], [(1, 2, 3)])) == 3  # fall back to row width
    assert column_count(_ok([], [])) is None  # nothing to go on
    assert column_count(_err()) is None


def test_shape_formatting() -> None:
    assert qi._shape(None) == "no-sql"
    assert qi._shape(_err()) == "ERR"
    assert qi._shape(_ok(["x"], [(1,), (2,)])) == "1col/2row"


# --- modes ------------------------------------------------------------------- #
QUESTION = {
    "question_id": 31,
    "db_id": "california_schools",
    "question": "What is the eligible free rate?",
    "evidence": "rate = free / enrollment",
    "SQL": "SELECT rate FROM frpm",
    "difficulty": "moderate",
}


def _patch_dataset_and_exec(monkeypatch, *, gold: InspectResult, pred: InspectResult) -> None:
    monkeypatch.setattr(qi, "load_questions", lambda: [QUESTION])

    async def fake_exec(db_id, sql, timeout_s=60):
        return gold if sql == QUESTION["SQL"] else pred

    monkeypatch.setattr(qi, "execute_for_inspection", fake_exec)


async def test_inspect_question_reports_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {
        "question_id": 31,
        "db_id": "california_schools",
        "difficulty": "moderate",
        "result": "answered",
        "correct": False,
        "tool_call_count": 1,
        "tool_calls": [
            {
                "step": 1,
                "name": "run_sql",
                "arguments": {"sql": "SELECT 1"},
                "result": "ok",
                "is_error": False,
            }
        ],
        "final_sql": "SELECT name, rate FROM frpm",
    }
    run_path = tmp_path / "run.jsonl"
    run_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    _patch_dataset_and_exec(
        monkeypatch,
        gold=_ok(["rate"], [(0.1,), (0.2,)]),
        pred=_ok(["name", "rate"], [("A", 0.1), ("B", 0.2)]),
    )

    await qi.inspect_question(run_path, 31)
    out = capsys.readouterr().out
    assert "What is the eligible free rate?" in out
    assert "rate = free / enrollment" in out
    assert "[1] run_sql" in out
    assert "Gold SQL" in out and "Final SQL" in out
    # 1 gold column vs 2 pred columns, same 2 rows.
    assert "same column count: no; same row count: yes" in out


async def test_inspect_question_no_sql_is_na(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {
        "question_id": 31,
        "db_id": "california_schools",
        "difficulty": "moderate",
        "result": "no_sql",
        "correct": False,
        "tool_call_count": 0,
        "tool_calls": [],
        "final_sql": None,
    }
    run_path = tmp_path / "run.jsonl"
    run_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    _patch_dataset_and_exec(monkeypatch, gold=_ok(["rate"], [(0.1,)]), pred=_err())

    await qi.inspect_question(run_path, 31)
    out = capsys.readouterr().out
    assert "(none — no_sql)" in out
    assert "same column count: n/a; same row count: n/a" in out


async def test_inspect_question_unknown_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    run_path = tmp_path / "run.jsonl"
    run_path.write_text(json.dumps({"question_id": 1, "db_id": "x"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(qi, "load_questions", list)
    with pytest.raises(SystemExit):
        await qi.inspect_question(run_path, 999)


async def test_all_wrong_lists_only_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    records = [
        {
            "question_id": 11,
            "db_id": "california_schools",
            "difficulty": "simple",
            "correct": True,
            "tool_call_count": 1,
            "final_sql": "SELECT a FROM t",
        },
        {
            "question_id": 31,
            "db_id": "california_schools",
            "difficulty": "moderate",
            "correct": False,
            "tool_call_count": 2,
            "final_sql": "SELECT a, b FROM t",
        },
    ]
    run_path = tmp_path / "run.jsonl"
    run_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        qi, "load_questions", lambda: [{"question_id": 31, "SQL": "SELECT a FROM t"}]
    )

    async def fake_exec(db_id, sql, timeout_s=60):
        return _ok(["a"], [(1,)]) if sql == "SELECT a FROM t" else _ok(["a", "b"], [(1, 2)])

    monkeypatch.setattr(qi, "execute_for_inspection", fake_exec)

    await qi.all_wrong(run_path)
    out = capsys.readouterr().out
    assert "1 record(s) with correct=False" in out
    assert "q31" in out
    assert "q11" not in out  # the correct record is excluded
    assert "gold=1col/1row" in out
    assert "pred=2col/1row" in out


async def test_inspect_shows_final_turn_failed_and_failed_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {
        "question_id": 31,
        "db_id": "california_schools",
        "difficulty": "moderate",
        "result": "final_turn_failed",
        "correct": False,
        "tool_call_count": 10,
        "tool_calls": [],
        "final_sql": None,
        "failed_generation": '{"name": "run_sql", "arguments": {"sql": "SELECT 1 FROM frpm"}}',
    }
    run_path = tmp_path / "run.jsonl"
    run_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    _patch_dataset_and_exec(monkeypatch, gold=_ok(["rate"], [(0.1,)]), pred=_err())

    await qi.inspect_question(run_path, 31)
    out = capsys.readouterr().out
    assert "final_turn_failed" in out  # the result is shown
    assert "Failed generation" in out  # the failed_generation block appears
    assert "SELECT 1 FROM frpm" in out
    # No predicted SQL to re-run, so the comparison is n/a.
    assert "same column count: n/a; same row count: n/a" in out
