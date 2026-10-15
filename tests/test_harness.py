"""The eval harness: SQL extraction and the dry-run agent loop (no real API, no DB).

The Anthropic client is faked with scripted responders, ``scoring.is_correct`` is
monkeypatched, and SQL execution is replaced with a canned result, so these tests
touch neither the network nor the database. The MCP side is real: a live in-memory
client to the main server, whose list_databases tool needs no database connection.
"""

from __future__ import annotations

import json
from collections import Counter

import httpx2
import openai
import pytest
from fastmcp import Client

from eval import harness, scoring
from eval.harness import (
    ExecResult,
    FakeAnthropic,
    FakeOpenAI,
    FakeOpenAICompletionDetails,
    FakeOpenAIUsage,
    FakeResponse,
    InfrastructureError,
    ProviderError,
    extract_last_sql,
    openai_text_completion,
    openai_tool_completion,
    rescore_file,
    run_eval,
    run_question,
    text_block,
    to_openai_tools,
    tool_use_block,
)
from queryguard.server import build_server

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

QUESTION = {
    "question_id": 999,
    "db_id": "superhero",
    "question": "How many superheroes are there?",
    "evidence": "count the rows",
    "SQL": "SELECT count(*) FROM superhero",
    "difficulty": "simple",
}


async def _fake_exec(db_id: str, sql: str, timeout_s: int = 60) -> ExecResult:
    return ExecResult("ok", [(42,)], 1, None)


def _exec_failing(*failing_sql: str, error: str = "boom"):
    """Fake executor that errors (with ``error``) for the given SQL and succeeds otherwise."""

    async def run(db_id: str, sql: str, timeout_s: int = 60) -> ExecResult:
        if sql in failing_sql:
            return ExecResult("error", None, None, error)
        return ExecResult("ok", [(42,)], 1, None)

    return run


def _exec_all_infra(error: str = "connection refused"):
    """Fake executor where every query fails as if the database were unreachable."""

    async def run(db_id: str, sql: str, timeout_s: int = 60) -> ExecResult:
        return ExecResult("error", None, None, error)

    return run


async def _run(
    responder,
    monkeypatch: pytest.MonkeyPatch,
    *,
    execute_sql=_fake_exec,
    scorer=lambda pred, gold: True,
    server=None,
) -> dict:
    monkeypatch.setenv("BIRD_DBS", QUESTION["db_id"])
    monkeypatch.setattr(scoring, "is_correct", scorer)
    return await run_question(
        QUESTION,
        condition="main",
        model="test-model",
        client=FakeAnthropic(responder),
        server=server if server is not None else build_server(),
        prices={},
        execute_sql=execute_sql,
    )


# --- SQL extraction ---------------------------------------------------------- #
def test_extract_last_sql_picks_last_block() -> None:
    text = "First attempt:\n```sql\nSELECT 1\n```\nNo wait, this one:\n```sql\nSELECT 2\n```\n"
    assert extract_last_sql(text) == "SELECT 2"


def test_extract_last_sql_none_when_missing() -> None:
    assert extract_last_sql("no code block here") is None
    assert extract_last_sql("") is None


# --- dry-run agent loop: three scenarios ------------------------------------- #
async def test_normal_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def responder(messages, tools, tool_choice):
        if harness._has_tool_result(messages):
            return FakeResponse([text_block("Final:\n```sql\nSELECT 1\n```")], "end_turn")
        return FakeResponse([tool_use_block("c1", "list_databases", {})], "tool_use")

    record = await _run(responder, monkeypatch)
    assert record["result"] == "answered"
    assert record["final_sql"] == "SELECT 1"
    assert record["tool_call_count"] == 1
    assert record["tool_calls"][0]["name"] == "list_databases"
    assert record["correct"] is True
    assert record["pred"]["status"] == "ok"


async def test_answer_with_no_sql_block(monkeypatch: pytest.MonkeyPatch) -> None:
    def responder(messages, tools, tool_choice):
        return FakeResponse([text_block("I am not sure how to answer that.")], "end_turn")

    record = await _run(responder, monkeypatch)
    assert record["result"] == "no_sql"
    assert record["final_sql"] is None
    assert record["tool_call_count"] == 0
    assert record["pred"]["status"] == "skipped"
    # Accuracy rule: no SQL produced -> wrong (scoring is not consulted).
    assert record["correct"] is False


async def test_tool_call_cap_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    def responder(messages, tools, tool_choice):
        if harness._is_forced_final(tool_choice):
            return FakeResponse([text_block("Forced:\n```sql\nSELECT 3\n```")], "end_turn")
        return FakeResponse([tool_use_block("c", "list_databases", {})], "tool_use")

    record = await _run(responder, monkeypatch)
    assert record["tool_call_count"] == harness.MAX_TOOL_CALLS
    assert record["result"] == "answered"
    assert record["final_sql"] == "SELECT 3"


# --- accuracy rule (step 2) -------------------------------------------------- #
def _answer_sql(sql: str):
    """Responder that immediately answers with ``sql`` in a fenced block."""

    def responder(messages, tools, tool_choice):
        return FakeResponse([text_block(f"```sql\n{sql}\n```")], "end_turn")

    return responder


async def test_predicted_sql_execution_failure_is_incorrect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Gold runs fine; the model's SQL fails to execute -> wrong, scoring not called.
    def boom_scorer(pred, gold):
        raise AssertionError("scoring must not be called when predicted SQL fails")

    record = await _run(
        _answer_sql("SELECT bad"),
        monkeypatch,
        execute_sql=_exec_failing("SELECT bad"),
        scorer=boom_scorer,
    )
    assert record["result"] == "answered"
    assert record["pred"]["status"] == "error"
    assert record["correct"] is False


async def test_gold_sql_failure_is_gold_error_and_null(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom_scorer(pred, gold):
        raise AssertionError("scoring must not be called when gold SQL fails")

    record = await _run(
        _answer_sql("SELECT 1"),
        monkeypatch,
        execute_sql=_exec_failing(QUESTION["SQL"]),  # gold fails
        scorer=boom_scorer,
    )
    assert record["result"] == "gold_error"
    assert record["correct"] is None
    assert "gold SQL failed" in capsys.readouterr().err


async def test_scoring_not_implemented_is_null(monkeypatch: pytest.MonkeyPatch) -> None:
    def not_implemented(pred, gold):
        raise NotImplementedError

    record = await _run(_answer_sql("SELECT 1"), monkeypatch, scorer=not_implemented)
    assert record["result"] == "answered"
    assert record["gold"]["status"] == "ok"
    assert record["pred"]["status"] == "ok"
    assert record["correct"] is None


# --- rescore (step 3) -------------------------------------------------------- #
async def test_rescore_updates_correct(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(scoring, "is_correct", lambda pred, gold: True)
    # Two records with deliberately stale verdicts; question_ids must exist in the
    # dataset so rescore can look up their gold SQL.
    run_path = tmp_path / "baseline_test_run.jsonl"
    stale = [
        {
            "question_id": 11,
            "db_id": "california_schools",
            "final_sql": "SELECT 1",
            "result": "no_sql",
            "correct": None,
            "gold": {"status": "error", "error": "stale", "row_count": None},
            "pred": {"status": "skipped", "error": None, "row_count": None},
        },
        {
            "question_id": 31,
            "db_id": "california_schools",
            "final_sql": None,
            "result": "answered",
            "correct": True,
            "gold": {"status": "ok", "error": None, "row_count": 1},
            "pred": {"status": "ok", "error": None, "row_count": 1},
        },
    ]
    run_path.write_text("\n".join(json.dumps(r) for r in stale) + "\n", encoding="utf-8")

    out_path = await rescore_file(run_path, execute_sql=_fake_exec)
    assert out_path == tmp_path / "baseline_test_run_rescored.jsonl"

    rescored = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    # Record 1: real SQL now runs (gold ok, pred ok, scorer True) -> answered / True.
    assert rescored[0]["result"] == "answered"
    assert rescored[0]["correct"] is True
    assert rescored[0]["pred"]["status"] == "ok"
    # Record 2: no stored final_sql -> no_sql / False.
    assert rescored[1]["result"] == "no_sql"
    assert rescored[1]["correct"] is False
    # The original file is left untouched.
    assert json.loads(run_path.read_text(encoding="utf-8").splitlines()[0])["correct"] is None


# --- scorer bugs must never discard a paid result (step 2) ------------------- #
async def test_non_bool_scoring_is_scoring_error(monkeypatch: pytest.MonkeyPatch) -> None:
    record = await _run(_answer_sql("SELECT 1"), monkeypatch, scorer=lambda pred, gold: None)
    assert record["result"] == "scoring_error"
    assert record["correct"] is None
    assert "must return exactly True or False" in record["scoring_error"]
    # The paid API result is preserved: the record is complete.
    assert record["pred"]["status"] == "ok"
    assert record["gold"]["status"] == "ok"


async def test_run_eval_live_path_writes_scoring_error_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Full run_eval path (not just run_question): a scorer that raises must not
    # discard the record — it is written with result "scoring_error" and the run
    # finishes normally (a scorer bug is not a reason to stop).
    monkeypatch.setattr(harness, "RUNS_DIR", tmp_path)
    monkeypatch.setenv("BIRD_DBS", "california_schools")
    monkeypatch.setenv("EVAL_PROVIDER", "anthropic")
    monkeypatch.setenv("EVAL_MODEL", "dry-run")

    def raiser(pred, gold):
        raise ValueError("scorer boom")

    monkeypatch.setattr(scoring, "is_correct", raiser)
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"question_ids": [11]}), encoding="utf-8")

    out = await run_eval(
        condition="main", sample_path=sample, n=1, dry_run=True, execute_sql=_fake_exec
    )
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["result"] == "scoring_error"
    assert records[0]["correct"] is None
    assert "ValueError" in records[0]["scoring_error"]


async def test_scoring_exception_is_scoring_error_and_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def raiser(pred, gold):
        raise ValueError("boom")

    monkeypatch.setattr(scoring, "is_correct", raiser)
    run_path = tmp_path / "baseline_test_run.jsonl"
    run_path.write_text(
        json.dumps(
            {
                "question_id": 11,
                "db_id": "california_schools",
                "final_sql": "SELECT 1",
                "result": "answered",
                "correct": None,
                "gold": {"status": "ok", "error": None, "row_count": 1},
                "pred": {"status": "ok", "error": None, "row_count": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    out_path = await rescore_file(run_path, execute_sql=_fake_exec)
    rescored = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    # The scorer blew up, yet the record still made it to disk, flagged for retry.
    assert len(rescored) == 1
    assert rescored[0]["result"] == "scoring_error"
    assert rescored[0]["correct"] is None
    assert "ValueError" in rescored[0]["scoring_error"]


async def test_rescore_recovers_scoring_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(scoring, "is_correct", lambda pred, gold: True)
    run_path = tmp_path / "baseline_test_run.jsonl"
    run_path.write_text(
        json.dumps(
            {
                "question_id": 11,
                "db_id": "california_schools",
                "final_sql": "SELECT 1",
                "result": "scoring_error",
                "correct": None,
                "scoring_error": "TypeError: old scorer bug",
                "gold": {"status": "ok", "error": None, "row_count": 1},
                "pred": {"status": "ok", "error": None, "row_count": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    out_path = await rescore_file(run_path, execute_sql=_fake_exec)
    rescored = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    # With a working scorer, the scoring_error record becomes a definite verdict.
    assert rescored[0]["result"] == "answered"
    assert rescored[0]["correct"] is True
    assert rescored[0]["scoring_error"] is None
    # The original file is left untouched.
    assert json.loads(run_path.read_text(encoding="utf-8").splitlines()[0])["correct"] is None


# --- infrastructure failures must never be scored as model failures (step 3) --- #
def _infra_tool_server():
    """A tiny MCP server whose only tool fails as if the database were down."""
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    server = FastMCP(name="infra-test")

    @server.tool(name="list_databases", description="stand-in that hits an outage")
    async def list_databases() -> list[str]:
        raise ToolError("could not connect to server: Connection refused")

    return server


async def test_pred_connection_refused_stops_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # Gold runs; the model's SQL execution hits a database outage -> stop, don't score.
    infra_msg = "[WinError 1225] The remote computer refused the network connection"
    with pytest.raises(InfrastructureError):
        await _run(
            _answer_sql("SELECT pred"),
            monkeypatch,
            execute_sql=_exec_failing("SELECT pred", error=infra_msg),
        )


async def test_tool_result_connection_error_stops_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def responder(messages, tools, tool_choice):
        return FakeResponse([tool_use_block("c1", "list_databases", {})], "tool_use")

    # The tool result carries a connectivity error -> stop before scoring anything.
    with pytest.raises(InfrastructureError):
        await _run(responder, monkeypatch, server=_infra_tool_server())


async def test_normal_sql_error_recorded_as_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # A plain SQL error (not an outage) is still a wrong answer, and is recorded.
    record = await _run(
        _answer_sql("SELECT oops"),
        monkeypatch,
        execute_sql=_exec_failing("SELECT oops", error='column "oops" does not exist'),
    )
    assert record["result"] == "answered"
    assert record["pred"]["status"] == "error"
    assert record["correct"] is False


async def test_run_eval_stops_on_outage_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness, "RUNS_DIR", tmp_path)
    monkeypatch.setenv("BIRD_DBS", "california_schools")
    monkeypatch.setenv("EVAL_MODEL", "dry-run")
    monkeypatch.setattr(scoring, "is_correct", lambda pred, gold: True)
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"seed": 42, "size": 1, "question_ids": [11]}), encoding="utf-8")

    with pytest.raises(SystemExit):
        await run_eval(
            condition="main",
            sample_path=sample,
            n=1,
            dry_run=True,
            execute_sql=_exec_all_infra(),
        )

    # No record was written for the question that hit the outage.
    written = list(tmp_path.glob("*.jsonl"))
    assert all(path.read_text(encoding="utf-8") == "" for path in written)
    # The operator is told what happened and exactly how to resume.
    err = capsys.readouterr().err
    assert "INFRASTRUCTURE FAILURE" in err
    assert "--resume" in err


async def test_rescore_stops_on_outage(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(scoring, "is_correct", lambda pred, gold: True)
    run_path = tmp_path / "run.jsonl"
    run_path.write_text(
        json.dumps(
            {
                "question_id": 11,
                "db_id": "california_schools",
                "final_sql": "SELECT 1",
                "result": "answered",
                "correct": True,
                "gold": {"status": "ok", "error": None, "row_count": 1},
                "pred": {"status": "ok", "error": None, "row_count": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        await rescore_file(run_path, execute_sql=_exec_all_infra())
    # No rescored file is produced when the database is unreachable.
    assert not (tmp_path / "run_rescored.jsonl").exists()


# --- Groq provider (step 8) -------------------------------------------------- #
def _rate_limit_error(message: str = "rate limit reached", retry_after: str = "0", headers=None):
    hdrs = {"retry-after": retry_after}
    if headers:
        hdrs.update(headers)
    resp = httpx2.Response(429, headers=hdrs, request=httpx2.Request("POST", GROQ_URL))
    return openai.RateLimitError(message, response=resp, body=None)


def _status_error(status: int, message: str):
    resp = httpx2.Response(status, request=httpx2.Request("POST", GROQ_URL))
    return openai.APIStatusError(message, response=resp, body=None)


async def _run_groq(
    responder,
    monkeypatch: pytest.MonkeyPatch,
    *,
    execute_sql=_fake_exec,
    scorer=lambda pred, gold: True,
    headers=None,
    server=None,
) -> dict:
    monkeypatch.setenv("BIRD_DBS", QUESTION["db_id"])
    monkeypatch.setattr(scoring, "is_correct", scorer)
    return await run_question(
        QUESTION,
        condition="main",
        model="llama-3.3-70b-versatile",
        client=FakeOpenAI(responder, headers=headers or {}),
        server=server if server is not None else build_server(),
        prices={},
        provider="groq",
        execute_sql=execute_sql,
    )


def _answer_after_tool(sql: str, calls):
    """Groq responder: emit ``calls`` first, then a final SQL answer."""

    def responder(messages, tools, tool_choice):
        if any(m.get("role") == "tool" for m in messages) or tool_choice == "none":
            return openai_text_completion(f"```sql\n{sql}\n```")
        return openai_tool_completion(calls)

    return responder


async def test_openai_tool_format_conversion() -> None:
    async with Client(build_server()) as client:
        tools = to_openai_tools(await client.list_tools())
    assert {t["function"]["name"] for t in tools} == {
        "list_databases",
        "list_tables",
        "describe_table",
        "find_columns",
        "validate_sql",
        "execute_query",
    }
    for tool in tools:
        assert tool["type"] == "function"
        assert "description" in tool["function"]
        assert tool["function"]["parameters"]["type"] == "object"
        assert "cache_control" not in tool  # no caching on the Groq path


async def test_groq_one_tool_call_then_final_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    record = await _run_groq(
        _answer_after_tool("SELECT 1", [("c1", "list_databases", "{}")]), monkeypatch
    )
    assert record["provider"] == "groq"
    assert record["final_sql"] == "SELECT 1"
    assert record["tool_call_count"] == 1
    assert record["tool_calls"][0]["name"] == "list_databases"
    assert record["usage"]["prompt_tokens"] > 0
    assert record["correct"] is True


async def test_groq_several_tool_calls_in_one_message(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = [("c1", "list_databases", "{}"), ("c2", "list_databases", "{}")]
    record = await _run_groq(_answer_after_tool("SELECT 1", calls), monkeypatch)
    # Each tool call in the single message is run and counted.
    assert record["tool_call_count"] == 2
    assert len(record["tool_calls"]) == 2
    # Both results are sent back as role "tool" messages with their ids.
    tool_ids = [m["tool_call_id"] for m in record["transcript"] if m.get("role") == "tool"]
    assert tool_ids == ["c1", "c2"]


async def test_groq_invalid_tool_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    record = await _run_groq(
        _answer_after_tool("SELECT 1", [("c1", "list_databases", "{not valid json")]),
        monkeypatch,
    )
    assert record["tool_call_count"] == 1  # the bad call still counts
    bad = record["tool_calls"][0]
    assert bad["is_error"] is True
    assert "Invalid JSON" in bad["result"]
    assert record["final_sql"] == "SELECT 1"  # the run continued, no crash


async def test_groq_429_retry_after_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def responder(messages, tools, tool_choice):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _rate_limit_error("Rate limit reached for requests per minute", retry_after="0")
        return openai_text_completion("```sql\nSELECT 1\n```")

    record = await _run_groq(responder, monkeypatch)
    assert attempts["n"] == 2  # waited once, then succeeded
    assert record["final_sql"] == "SELECT 1"


async def test_groq_rate_limit_headers_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = {"x-ratelimit-remaining-requests": "42", "x-ratelimit-remaining-tokens": "9999"}

    def responder(messages, tools, tool_choice):
        return openai_text_completion("```sql\nSELECT 1\n```")

    record = await _run_groq(responder, monkeypatch, headers=headers)
    assert record["rate_limit"]["remaining_requests"] == "42"
    assert record["rate_limit"]["remaining_tokens"] == "9999"


async def test_groq_daily_limit_stops_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def responder(messages, tools, tool_choice):
        raise _rate_limit_error("Rate limit reached for tokens per day")

    with pytest.raises(ProviderError):
        await _run_groq(responder, monkeypatch)


async def test_groq_request_too_large_stops_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def responder(messages, tools, tool_choice):
        raise _status_error(413, "Request Entity Too Large")

    with pytest.raises(ProviderError):
        await _run_groq(responder, monkeypatch)


# --- 429 daily-vs-per-minute classification (bug fix 2026-10-11) -------------- #
# Realistic Groq 429 bodies. All end with the "Upgrade to Dev Tier today" blurb, so
# the word "today"/"day" alone must NOT mark a daily stop — only TPD/RPD do.
_GROQ_PREFIX = (
    "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` "
    "service tier `on_demand` on "
)
_GROQ_UPGRADE = (
    " Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing"
)


def _raise_once_then_answer(error):
    """Groq responder: raise ``error`` on the first call, then return a final SQL answer."""
    attempts = {"n": 0}

    def responder(messages, tools, tool_choice):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise error
        return openai_text_completion("```sql\nSELECT 1\n```")

    return responder, attempts


async def test_groq_429_per_minute_tpm_with_today_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    # (a) A per-minute TPM limit whose text contains "today" must wait and retry.
    msg = (
        _GROQ_PREFIX + "tokens per minute (TPM): Limit 8000, Used 7999, Requested 500. "
        "Please try again in 262.5ms." + _GROQ_UPGRADE
    )
    responder, attempts = _raise_once_then_answer(_rate_limit_error(msg, retry_after="0"))
    record = await _run_groq(responder, monkeypatch)
    assert attempts["n"] == 2  # waited once, did not stop the run
    assert record["final_sql"] == "SELECT 1"


async def test_groq_429_tokens_per_day_tpd_stops_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # (b) An explicit tokens-per-day (TPD) limit is a hard daily stop.
    msg = (
        _GROQ_PREFIX + "tokens per day (TPD): Limit 200000, Used 199037, Requested 2183. "
        "Please try again in 11m24.288s." + _GROQ_UPGRADE
    )

    def responder(messages, tools, tool_choice):
        raise _rate_limit_error(msg)

    with pytest.raises(ProviderError):
        await _run_groq(responder, monkeypatch)


async def test_groq_429_requests_per_day_rpd_stops_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # (c) An explicit requests-per-day (RPD) limit is a hard daily stop.
    msg = (
        _GROQ_PREFIX + "requests per day (RPD): Limit 1000, Used 1000, Requested 1. "
        "Please try again in 5m0s." + _GROQ_UPGRADE
    )

    def responder(messages, tools, tool_choice):
        raise _rate_limit_error(msg)

    with pytest.raises(ProviderError):
        await _run_groq(responder, monkeypatch)


async def test_groq_429_reconstructed_q207_tpm_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    # (d) The exact 2026-10-09 14:00 (q207) 429 text was NOT preserved in any log;
    # this reconstructs RUN_LOG's description (per-minute TPM, Limit 8000, retry
    # 262.5ms) to assert that stop would now wait instead of ending the run.
    msg = (
        _GROQ_PREFIX + "tokens per minute (TPM): Limit 8000, Used 8000, Requested 1183. "
        "Please try again in 262.5ms." + _GROQ_UPGRADE
    )
    responder, attempts = _raise_once_then_answer(_rate_limit_error(msg, retry_after="0"))
    record = await _run_groq(responder, monkeypatch)
    assert attempts["n"] == 2
    assert record["final_sql"] == "SELECT 1"


async def test_run_eval_groq_daily_limit_stops_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(harness, "RUNS_DIR", tmp_path)
    monkeypatch.setenv("BIRD_DBS", "california_schools")
    monkeypatch.setenv("EVAL_PROVIDER", "groq")
    monkeypatch.setenv("EVAL_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setattr(scoring, "is_correct", lambda pred, gold: True)

    def responder(messages, tools, tool_choice):
        raise _rate_limit_error("Rate limit reached for tokens per day")

    monkeypatch.setattr(harness, "_build_client", lambda provider, dry_run: FakeOpenAI(responder))
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"seed": 42, "size": 1, "question_ids": [11]}), encoding="utf-8")

    with pytest.raises(SystemExit):
        await run_eval(condition="main", sample_path=sample, n=1, dry_run=True)

    written = list(tmp_path.glob("*.jsonl"))
    assert all(path.read_text(encoding="utf-8") == "" for path in written)
    err = capsys.readouterr().err
    assert "PROVIDER LIMIT" in err
    assert "--resume" in err


# --- SQL extraction fallback (step 3) ---------------------------------------- #
def test_extract_sql_fallback_plain_block_starting_with_select() -> None:
    assert extract_last_sql("Here:\n```\nSELECT 1\n```") == "SELECT 1"


def test_extract_sql_fallback_plain_block_starting_with_with() -> None:
    result = extract_last_sql("```\nWITH t AS (SELECT 1) SELECT * FROM t\n```")
    assert result is not None and result.lower().startswith("with")


def test_extract_sql_fallback_requires_select_or_with() -> None:
    assert extract_last_sql("```\nprint('not sql')\n```") is None


def test_extract_sql_prefers_sql_block_over_plain() -> None:
    text = "```\nSELECT wrong\n```\n```sql\nSELECT right\n```"
    assert extract_last_sql(text) == "SELECT right"


# --- Groq reasoning / usage / rate-limit follow-ups (Phase 1.5) -------------- #
async def _run_groq_capture(
    responder,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str = "openai/gpt-oss-120b",
    headers=None,
    scorer=lambda pred, gold: True,
    execute_sql=_fake_exec,
) -> tuple[dict, FakeOpenAI]:
    """Run one Groq question and return (record, client) so sent kwargs are visible."""
    monkeypatch.setenv("BIRD_DBS", QUESTION["db_id"])
    monkeypatch.setattr(scoring, "is_correct", scorer)
    client = FakeOpenAI(responder, headers=headers or {})
    record = await run_question(
        QUESTION,
        condition="main",
        model=model,
        client=client,
        server=build_server(),
        prices={},
        provider="groq",
        execute_sql=execute_sql,
    )
    return record, client


def _final_sql_responder(sql: str = "SELECT 1", usage=None):
    def responder(messages, tools, tool_choice):
        return openai_text_completion(f"```sql\n{sql}\n```", usage=usage)

    return responder


async def test_groq_reasoning_effort_default_medium(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_REASONING_EFFORT", raising=False)
    record, client = await _run_groq_capture(
        _answer_after_tool("SELECT 1", [("c1", "list_databases", "{}")]), monkeypatch
    )
    assert record["reasoning_effort"] == "medium"
    # Sent on every request (the tool-call turn and the follow-up).
    assert len(client.requests) >= 2
    assert all(req.get("reasoning_effort") == "medium" for req in client.requests)


async def test_groq_reasoning_effort_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_REASONING_EFFORT", "high")
    record, client = await _run_groq_capture(_final_sql_responder(), monkeypatch)
    assert record["reasoning_effort"] == "high"
    assert client.requests[0]["reasoning_effort"] == "high"


async def test_groq_no_reasoning_effort_for_non_gpt_oss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_REASONING_EFFORT", "high")
    record, client = await _run_groq_capture(
        _final_sql_responder(), monkeypatch, model="qwen/qwen3.8-27b"
    )
    assert record["reasoning_effort"] is None
    assert all("reasoning_effort" not in req for req in client.requests)


async def test_groq_reasoning_tokens_recorded_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = FakeOpenAIUsage(
        prompt_tokens=100,
        completion_tokens=40,
        completion_tokens_details=FakeOpenAICompletionDetails(reasoning_tokens=25),
    )
    record, _ = await _run_groq_capture(_final_sql_responder(usage=usage), monkeypatch)
    assert record["usage"]["reasoning_tokens"] == 25
    assert record["usage"]["completion_tokens"] == 40  # kept separate from reasoning


async def test_groq_rate_limit_includes_limit_and_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-remaining-requests": "998",
        "x-ratelimit-limit-tokens": "500000",
        "x-ratelimit-remaining-tokens": "499000",
    }
    record, _ = await _run_groq_capture(_final_sql_responder(), monkeypatch, headers=headers)
    rl = record["rate_limit"]
    assert rl["limit_requests"] == "1000"
    assert rl["remaining_requests"] == "998"
    assert rl["limit_tokens"] == "500000"
    assert rl["remaining_tokens"] == "499000"


async def test_groq_max_request_prompt_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    small = FakeOpenAIUsage(prompt_tokens=100, completion_tokens=5)
    big = FakeOpenAIUsage(prompt_tokens=250, completion_tokens=5)

    def responder(messages, tools, tool_choice):
        if any(m.get("role") == "tool" for m in messages):
            return openai_text_completion("```sql\nSELECT 1\n```", usage=big)
        return openai_tool_completion([("c1", "list_databases", "{}")], usage=small)

    record, _ = await _run_groq_capture(responder, monkeypatch)
    # Largest single request, not the sum.
    assert record["max_request_prompt_tokens"] == 250
    assert record["usage"]["prompt_tokens"] == 350  # total still accumulated


# --- Dev set, column instruction, prompt versioning (Phase 1.6) -------------- #
COLUMN_INSTRUCTION = "Select only the columns the question asks for"


def _dev_path():
    return harness.PROJECT_ROOT / "eval" / "dev_10.json"


def _sample50_ids() -> set[int]:
    return set(harness.load_sample(harness.PROJECT_ROOT / "eval" / "sample_50.json"))


def test_dev_sample_shape_no_overlap_and_deterministic() -> None:
    questions = harness.load_questions()
    by_id = {q["question_id"]: q for q in questions}
    exclude = _sample50_ids()

    committed = harness.load_sample(_dev_path())
    rebuilt = harness.build_dev_sample(questions, exclude)
    again = harness.build_dev_sample(questions, exclude)

    assert len(committed) == 10
    assert rebuilt == again == committed  # same ids on re-creation with seed 7
    assert set(committed) & exclude == set()  # no overlap with sample_50
    split = Counter(by_id[i]["difficulty"] for i in committed)
    assert dict(split) == {"simple": 3, "moderate": 5, "challenging": 2}


def test_create_dev_sample_never_overwrites(tmp_path) -> None:
    questions = harness.load_questions()
    exclude = _sample50_ids()
    path = tmp_path / "dev.json"

    harness.create_dev_sample(path, questions, exclude)
    # Tamper with the file; a second call must NOT regenerate it.
    path.write_text(json.dumps({"question_ids": [1, 2, 3]}), encoding="utf-8")
    assert harness.create_dev_sample(path, questions, exclude) == [1, 2, 3]


def test_load_sample_missing_errors(tmp_path) -> None:
    with pytest.raises(SystemExit):
        harness.load_sample(tmp_path / "nope.json")


async def test_run_eval_never_creates_missing_sample(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("EVAL_PROVIDER", "anthropic")
    monkeypatch.setenv("EVAL_MODEL", "dry-run")
    missing = tmp_path / "missing.json"
    with pytest.raises(SystemExit):
        await run_eval(condition="main", sample_path=missing, dry_run=True)
    assert not missing.exists()  # --sample never creates a sample file


def test_both_conditions_have_column_instruction() -> None:
    final_marker = "reply with exactly one final SQL query"
    for condition in ("baseline", "main"):
        prompt = harness.system_prompt("superhero", "hint", condition)
        assert COLUMN_INSTRUCTION in prompt
        # Placed before the final-answer instruction.
        assert prompt.index(COLUMN_INSTRUCTION) < prompt.index(final_marker)
    # The main-only line is still present only in main.
    assert "Always pass database" in harness.system_prompt("superhero", "hint", "main")
    assert "Always pass database" not in harness.system_prompt("superhero", "hint", "baseline")


def test_prompt_version_is_short_hex_hash() -> None:
    version = harness.prompt_version()
    assert len(version) == 8
    assert all(c in "0123456789abcdef" for c in version)


def test_prompt_version_changes_when_template_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    before = harness.prompt_version()
    original = harness.system_prompt
    monkeypatch.setattr(
        harness, "system_prompt", lambda db, ev, cond: original(db, ev, cond) + " (reworded)"
    )
    assert harness.prompt_version() != before


async def test_record_includes_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    record = await _run(_answer_sql("SELECT 1"), monkeypatch)
    assert record["prompt_version"] == harness.prompt_version()


async def test_run_eval_filename_includes_prompt_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(harness, "RUNS_DIR", tmp_path)
    monkeypatch.setenv("BIRD_DBS", "california_schools")
    monkeypatch.setenv("EVAL_PROVIDER", "anthropic")
    monkeypatch.setenv("EVAL_MODEL", "dry-run")
    monkeypatch.setattr(scoring, "is_correct", lambda pred, gold: True)
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"question_ids": [11]}), encoding="utf-8")

    out = await run_eval(
        condition="main", sample_path=sample, n=1, dry_run=True, execute_sql=_fake_exec
    )
    assert out.name.startswith(f"main_anthropic_dry-run_{harness.prompt_version()}_")


def _write_resume_file(tmp_path, record: dict):
    path = tmp_path / "old_run.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


async def _run_eval_resume(monkeypatch, tmp_path, resume_path):
    monkeypatch.setenv("EVAL_PROVIDER", "anthropic")
    monkeypatch.setenv("EVAL_MODEL", "dry-run")
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"question_ids": [11]}), encoding="utf-8")
    await run_eval(condition="main", sample_path=sample, resume=str(resume_path), dry_run=True)


async def test_resume_refuses_different_prompt_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    resume_path = _write_resume_file(tmp_path, {"question_id": 11, "prompt_version": "deadbeef"})
    with pytest.raises(SystemExit):
        await _run_eval_resume(monkeypatch, tmp_path, resume_path)


async def test_resume_refuses_missing_prompt_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    resume_path = _write_resume_file(tmp_path, {"question_id": 11})  # no prompt_version
    with pytest.raises(SystemExit):
        await _run_eval_resume(monkeypatch, tmp_path, resume_path)


# --- final-turn retry + no run crashes (Phase 1.7) --------------------------- #
def _tool_use_failed_error(
    failed_generation: str = '{"name": "run_sql", "arguments": {"sql": "SELECT 1"}}',
):
    body = {
        "error": {
            "message": "Tool choice is none, but model called a tool",
            "type": "invalid_request_error",
            "code": "tool_use_failed",
            "failed_generation": failed_generation,
        }
    }
    resp = httpx2.Response(400, request=httpx2.Request("POST", GROQ_URL))
    return openai.BadRequestError("tool_use_failed", response=resp, body=body)


def _cap_then_final(final_outcomes):
    """Groq responder: 10 tool calls (hits the cap), then scripted final-turn outcomes."""
    state = {"i": 0}

    def responder(messages, tools, tool_choice):
        if tool_choice == "none":
            outcome = final_outcomes[min(state["i"], len(final_outcomes) - 1)]
            state["i"] += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return openai_tool_completion([("c", "list_databases", "{}")])

    return responder


async def test_groq_final_turn_succeeds_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    responder = _cap_then_final(
        [_sdk_tool_use_failed_error(), openai_text_completion("```sql\nSELECT 99\n```")]
    )
    record, _ = await _run_groq_capture(responder, monkeypatch)
    assert record["tool_call_count"] == harness.MAX_TOOL_CALLS
    assert record["result"] == "answered"  # the 2nd attempt returned clean text
    assert record["final_sql"] == "SELECT 99"
    assert record["failed_generation"] is None


async def test_groq_final_turn_all_attempts_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    fg = '{"name": "run_sql", "arguments": {"sql": "SELECT COUNT(*) FROM cards"}}'
    responder = _cap_then_final([_sdk_tool_use_failed_error(fg)] * harness.FINAL_TURN_ATTEMPTS)
    record, _ = await _run_groq_capture(responder, monkeypatch)
    assert record["result"] == "final_turn_failed"
    assert record["correct"] is False
    assert record["final_sql"] is None
    assert record["failed_generation"] == fg  # stored for inspection only


# The exact failed_generation q32 produced (results/baseline_6b.errors.jsonl).
_Q32_FAILED_GENERATION = (
    '{"name": "run_sql", "arguments": {"sql":"SELECT * FROM frpm WHERE '
    '\\"District Code\\"=66 LIMIT 5;"}}'
)


def _sdk_tool_use_failed_error(failed_generation: str = _Q32_FAILED_GENERATION):
    """q32's 400 as the openai SDK actually builds it: body is the UNWRAPPED inner dict.

    openai 2.28.0's AsyncOpenAI._make_status_error does ``data = body.get("error", body)``
    and stores that in ``exc.body``, so the "error" envelope is already peeled off. This
    mirrors the real exception, unlike ``_tool_use_failed_error`` which keeps the envelope.
    """
    inner = {
        "message": "Tool choice is none, but model called a tool",
        "type": "invalid_request_error",
        "code": "tool_use_failed",
        "failed_generation": failed_generation,
    }
    resp = httpx2.Response(400, request=httpx2.Request("POST", GROQ_URL))
    return openai.BadRequestError("Error code: 400", response=resp, body=inner)


def test_is_tool_use_failed_on_real_sdk_error_body() -> None:
    # Before the _error_body fix this returned False (the error escaped to the errors file).
    exc = _sdk_tool_use_failed_error()
    assert harness._is_tool_use_failed(exc) is True
    assert harness._failed_generation(exc) == _Q32_FAILED_GENERATION


def test_is_tool_use_failed_on_envelope_error_body() -> None:
    # The one envelope-shaped case kept on purpose: _error_body must also unwrap a
    # full {"error": {...}} body, so detection works whichever shape the SDK hands us.
    # (The final-turn loop path on the real SDK shape is covered by the two tests above.)
    exc = _tool_use_failed_error()
    assert harness._is_tool_use_failed(exc) is True
    assert "run_sql" in (harness._failed_generation(exc) or "")


def _unexpected_then_ok_responder(state):
    """First call raises an unexpected (non-API) error; later calls answer with SQL."""

    def responder(messages, tools, tool_choice):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("kaboom unexpected")
        return openai_text_completion("```sql\nSELECT 1\n```")

    return responder


def _setup_groq_run_eval(monkeypatch, tmp_path, responder):
    monkeypatch.setattr(harness, "RUNS_DIR", tmp_path)
    monkeypatch.setenv("BIRD_DBS", "california_schools")
    monkeypatch.setenv("EVAL_PROVIDER", "groq")
    monkeypatch.setenv("EVAL_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setattr(scoring, "is_correct", lambda pred, gold: True)
    monkeypatch.setattr(harness, "_build_client", lambda provider, dry_run: FakeOpenAI(responder))
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"question_ids": [11, 31]}), encoding="utf-8")
    return sample


def _run_file_and_errors(tmp_path):
    err_files = list(tmp_path.glob("*.errors.jsonl"))
    run_files = [p for p in tmp_path.glob("*.jsonl") if not p.name.endswith(".errors.jsonl")]
    return run_files, err_files


async def test_unexpected_error_recorded_run_continues_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    sample = _setup_groq_run_eval(monkeypatch, tmp_path, _unexpected_then_ok_responder({"n": 0}))

    with pytest.raises(SystemExit) as exc_info:
        await run_eval(
            condition="main",
            sample_path=sample,
            concurrency=1,
            dry_run=True,
            execute_sql=_fake_exec,
        )
    assert exc_info.value.code != 0  # non-zero because a question errored

    run_files, err_files = _run_file_and_errors(tmp_path)
    assert len(err_files) == 1 and len(run_files) == 1
    errs = [json.loads(line) for line in err_files[0].read_text(encoding="utf-8").splitlines()]
    assert len(errs) == 1
    assert errs[0]["question_id"] == 11
    assert errs[0]["error_type"] == "ValueError"
    assert "kaboom unexpected" in errs[0]["message"]
    assert "Traceback" in errs[0]["traceback"]
    # The other question still produced a record.
    recs = [json.loads(line) for line in run_files[0].read_text(encoding="utf-8").splitlines()]
    assert [r["question_id"] for r in recs] == [31]
    err_out = capsys.readouterr().err
    assert "errored" in err_out
    assert "--resume" in err_out


async def test_error_summary_printed_even_when_run_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = {"n": 0}

    def responder(messages, tools, tool_choice):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("kaboom")  # q11: unexpected error
        raise _rate_limit_error("Rate limit reached for tokens per day")  # q31: daily -> stop

    sample = _setup_groq_run_eval(monkeypatch, tmp_path, responder)

    with pytest.raises(SystemExit) as exc_info:
        await run_eval(
            condition="main",
            sample_path=sample,
            concurrency=1,
            dry_run=True,
            execute_sql=_fake_exec,
        )
    assert exc_info.value.code == 2  # RunStopped exit code

    err_out = capsys.readouterr().err
    assert "errored" in err_out  # the error summary still printed
    assert "PROVIDER LIMIT" in err_out  # alongside the stop message
    _, err_files = _run_file_and_errors(tmp_path)
    errs = [json.loads(line) for line in err_files[0].read_text(encoding="utf-8").splitlines()]
    assert [e["question_id"] for e in errs] == [11]


def test_prompt_version_still_a16401d6() -> None:
    assert harness.prompt_version() == "a16401d6"
