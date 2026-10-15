"""Hand-checked tests for the Phase 6 analysis statistics (stdlib only)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.analyze_phase6 import (
    apply_tool_use_failed_rule,
    integrity_checks,
    load_tool_use_failed_ids,
    mcnemar_exact_two_sided,
    wilson_interval,
)


def test_mcnemar_b2_c10() -> None:
    # 158/4096 ≈ 0.038574
    assert abs(mcnemar_exact_two_sided(2, 10) - 158 / 4096) < 1e-12


def test_mcnemar_b0_c5() -> None:
    assert abs(mcnemar_exact_two_sided(0, 5) - 0.0625) < 1e-12


def test_mcnemar_b3_c3() -> None:
    assert mcnemar_exact_two_sided(3, 3) == 1.0


def test_mcnemar_no_discordant() -> None:
    assert mcnemar_exact_two_sided(0, 0) == 1.0


def test_wilson_upper_0_of_10() -> None:
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0
    assert abs(hi - 0.2775) < 1e-3


# --------------------------------------------------------------------------- #
# tool_use_failed -> no_sql rule (preregistered)
# --------------------------------------------------------------------------- #
_TMPL = {
    "model": "openai/gpt-oss-120b",
    "reasoning_effort": "medium",
    "prompt_version": "a16401d6",
}


def _rec(qid: int, condition: str, *, correct: bool = True, **extra) -> dict:
    rec = {
        "question_id": qid,
        "condition": condition,
        "difficulty": "simple",
        "db_id": "california_schools",
        "tool_call_count": 1,
        "result": "answered",
        "correct": correct,
        "gold": {"status": "ok", "error": None, "row_count": 1},
        "pred": {"status": "ok", "error": None, "row_count": 1},
        **_TMPL,
    }
    rec.update(extra)
    return rec


def test_load_tool_use_failed_ids(tmp_path) -> None:
    p = tmp_path / "baseline_6b.errors.jsonl"
    p.write_text(
        '{"question_id": 32, "error_type": "BadRequestError", "message": '
        "\"Error code: 400 - {'error': {'code': 'tool_use_failed', "
        "'message': 'Tool choice is none, but model called a tool'}}\"}\n"
        '{"question_id": 50, "error_type": "RuntimeError", "message": "some other failure"}\n',
        encoding="utf-8",
    )
    assert load_tool_use_failed_ids(p) == {32}


def test_load_tool_use_failed_ids_missing_file(tmp_path) -> None:
    assert load_tool_use_failed_ids(tmp_path / "nope.errors.jsonl") == set()


def test_tool_use_failed_missing_id_scored_no_sql() -> None:
    sample = [1, 2, 32]
    base = [_rec(1, "baseline"), _rec(2, "baseline")]  # 32 absent from the run file
    main = [_rec(1, "main"), _rec(2, "main"), _rec(32, "main")]
    meta = {32: {"difficulty": "moderate", "db_id": "card_games"}}

    base2, applied = apply_tool_use_failed_rule(base, {32}, sample, "baseline", meta, _TMPL)
    assert applied == [32]
    syn = next(r for r in base2 if r["question_id"] == 32)
    assert syn["result"] == "no_sql"
    assert syn["correct"] is False
    assert syn["_tool_use_failed_no_sql"] is True
    assert syn["difficulty"] == "moderate" and syn["db_id"] == "card_games"
    # A missing-but-tool_use_failed id is no longer an integrity failure.
    assert integrity_checks(base2, main, sample, allow_small=True) == []


def test_tool_use_failed_overrides_run_record() -> None:
    sample = [1, 32]
    base = [_rec(1, "baseline"), _rec(32, "baseline", correct=True)]  # a later resume record
    base2, applied = apply_tool_use_failed_rule(base, {32}, sample, "baseline", {}, _TMPL)
    assert applied == [32]
    recs32 = [r for r in base2 if r["question_id"] == 32]
    assert len(recs32) == 1  # the resume record is dropped, replaced by one no_sql record
    assert recs32[0]["result"] == "no_sql" and recs32[0]["correct"] is False


def test_missing_id_without_tool_use_failed_still_fails_integrity() -> None:
    sample = [1, 2, 99]
    base = [_rec(1, "baseline"), _rec(2, "baseline")]  # 99 absent, and NOT a tool_use_failed
    main = [_rec(1, "main"), _rec(2, "main"), _rec(99, "main")]
    base2, applied = apply_tool_use_failed_rule(base, set(), sample, "baseline", {}, _TMPL)
    assert applied == []
    failures = integrity_checks(base2, main, sample, allow_small=True)
    assert any("missing=[99]" in f for f in failures)
