"""Evaluation harness: run text-to-SQL questions through an MCP server + a model.

Two conditions share everything (model, prompt, timeout, row cap) except which
server the model talks to:

* ``baseline`` — ``eval.baseline_server``: one ``run_sql`` tool, schema in its
  description, no safety checks beyond the read-only role.
* ``main`` — ``queryguard.server``: the discovery tools (and, later, the safe
  query tool and the full safety stack).

For each question we open an in-memory MCP client to the chosen server, hand the
model its tools, and run a tool-use loop (temperature 0, at most 10 tool calls).
We then run both the model's SQL and the gold SQL as ``qg_reader`` and ask
``eval.scoring.is_correct`` to judge them. Every run is written as one JSON line.

Usage::

    uv run python -m eval.harness --condition baseline|main --sample eval/sample_50.json \\
        [--n 5] [--concurrency 2] [--resume <file>] [--dry-run]

``--dry-run`` swaps in a fake Anthropic client with canned responses and makes no
API calls (it still reads the local database to score SQL).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
import tomllib
import traceback
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import asyncpg
import openai
from fastmcp import Client

from eval import scoring
from eval.baseline_server import build_baseline_server
from queryguard.db import close_all, get_pool
from queryguard.server import build_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = PROJECT_ROOT / "data" / "mini_dev_postgresql.json"
PRICING_PATH = PROJECT_ROOT / "eval" / "pricing.toml"
RUNS_DIR = PROJECT_ROOT / "eval" / "runs"

MAX_TOOL_CALLS = 10
MAX_TOKENS = 2048
SAMPLE_SIZE = 50
SAMPLE_SEED = 42
API_MAX_RETRIES = 5
SCORING_TIMEOUT_S = 60

# Providers
ANTHROPIC = "anthropic"
GROQ = "groq"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Groq rate-limit handling: how many times to wait on a per-minute 429, and how
# many consecutive 5xx/connection errors to tolerate, before stopping the run.
MAX_RATE_LIMIT_WAITS = 20
MAX_SERVER_ERRORS = 5
# gpt-oss on Groq sometimes still calls a tool on the forced final turn
# (tool_choice="none"), which Groq rejects with a 400 tool_use_failed. Retry the
# identical final request this many times before giving up on that question.
FINAL_TURN_ATTEMPTS = 3

FINAL_PROMPT = (
    "You have reached the tool-call limit. Based on what you have already learned, "
    "reply now with exactly one final SQL query in a ```sql code block."
)


# --------------------------------------------------------------------------- #
# Questions and the stratified sample
# --------------------------------------------------------------------------- #
def load_questions() -> list[dict[str, Any]]:
    """Load all 500 BIRD mini-dev questions."""
    with open(DATA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def build_sample(
    questions: list[dict[str, Any]], size: int = SAMPLE_SIZE, seed: int = SAMPLE_SEED
) -> list[int]:
    """Pick ``size`` question_ids whose difficulty mix matches the full set.

    Uses the largest-remainder method so the per-difficulty counts sum to exactly
    ``size``, then samples within each difficulty with a fixed seed.
    """
    by_difficulty: dict[str, list[int]] = defaultdict(list)
    for q in questions:
        by_difficulty[q["difficulty"]].append(q["question_id"])

    total = len(questions)
    difficulties = sorted(by_difficulty)
    ideal = {d: len(by_difficulty[d]) * size / total for d in difficulties}
    counts = {d: int(ideal[d]) for d in difficulties}
    remainder = size - sum(counts.values())
    # Hand the leftover slots to the largest fractional parts.
    for d in sorted(difficulties, key=lambda d: ideal[d] - counts[d], reverse=True)[:remainder]:
        counts[d] += 1

    rng = random.Random(seed)
    chosen: list[int] = []
    for d in difficulties:
        ids = sorted(by_difficulty[d])
        chosen.extend(rng.sample(ids, min(counts[d], len(ids))))
    return sorted(chosen)


DEV_SAMPLE_SEED = 7
DEV_SAMPLE_COUNTS = {"simple": 3, "moderate": 5, "challenging": 2}


def build_dev_sample(
    questions: list[dict[str, Any]],
    exclude_ids: set[int],
    counts: dict[str, int] = DEV_SAMPLE_COUNTS,
    seed: int = DEV_SAMPLE_SEED,
) -> list[int]:
    """Pick a fixed-size dev set stratified by difficulty, excluding ``exclude_ids``.

    Deterministic for a given seed: samples within each difficulty (in a fixed
    difficulty order) from the pool of not-excluded question_ids.
    """
    by_difficulty: dict[str, list[int]] = defaultdict(list)
    for q in questions:
        if q["question_id"] not in exclude_ids:
            by_difficulty[q["difficulty"]].append(q["question_id"])

    rng = random.Random(seed)
    chosen: list[int] = []
    for difficulty in sorted(counts):  # fixed order so the draw is reproducible
        pool = sorted(by_difficulty[difficulty])
        wanted = counts[difficulty]
        if len(pool) < wanted:
            raise ValueError(
                f"Only {len(pool)} non-excluded {difficulty!r} questions; need {wanted}."
            )
        chosen.extend(rng.sample(pool, wanted))
    return sorted(chosen)


def load_sample(path: str | Path) -> list[int]:
    """Load a sample file's question_ids. Read-only: never creates or modifies it."""
    path = Path(path)
    if not path.exists():
        raise SystemExit(
            f"Sample file not found: {path}. --sample never creates or modifies sample "
            "files; create it first."
        )
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data["question_ids"] if isinstance(data, dict) else data


def create_dev_sample(
    path: str | Path, questions: list[dict[str, Any]], exclude_ids: set[int]
) -> list[int]:
    """Create the dev sample file once (never overwriting it) and return its ids."""
    path = Path(path)
    if path.exists():
        return load_sample(path)
    ids = build_dev_sample(questions, exclude_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "seed": DEV_SAMPLE_SEED,
                "size": len(ids),
                "excludes": "eval/sample_50.json",
                "question_ids": ids,
            },
            fh,
            indent=2,
        )
    return ids


# --------------------------------------------------------------------------- #
# Prompt, tool conversion, SQL extraction
# --------------------------------------------------------------------------- #
def system_prompt(db_id: str, evidence: str, condition: str) -> str:
    """The system prompt — identical in both conditions bar one main-only line."""
    text = (
        f'You are a data analyst answering a question about the PostgreSQL database "{db_id}". '
        "Use the tools to explore the database and test your queries. "
        f"Dataset hint: {evidence}. "
        "Select only the columns the question asks for, in the order the question mentions "
        "them; do not add extra columns such as IDs or names unless requested. "
        "When you are confident, reply with exactly one final SQL query in a ```sql code block."
    )
    if condition == "main":
        text += f' Always pass database="{db_id}" to tools that take a database argument.'
    return text


def prompt_version() -> str:
    """Short hash of the exact system-prompt template (incl. the main-condition line).

    Computed from the template with placeholder db_id/evidence, so it tracks the
    wording, not the per-question substitutions. Changes whenever the template text
    changes; stored in every record and in the run file name.
    """
    template = system_prompt("{db_id}", "{evidence}", "main")
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:8]


def to_anthropic_tools(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    """Convert MCP tool definitions to the Anthropic tools format.

    A ``cache_control`` marker on the last tool caches the whole tools block.
    """
    tools = [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.input_schema,
        }
        for t in mcp_tools
    ]
    if tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def to_openai_tools(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    """Convert MCP tool definitions to the OpenAI function-tool format (no caching)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema,
            },
        }
        for t in mcp_tools
    ]


_SQL_BLOCK = re.compile(r"```sql\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_ANY_BLOCK = re.compile(r"```(.*?)```", re.DOTALL)
_STARTS_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def extract_last_sql(text: str | None) -> str | None:
    """Extract the model's final SQL from its answer.

    Prefer the last ```sql fenced block. If there is none, fall back to the last
    plain ``` block, but only when its content begins with SELECT or WITH
    (case-insensitive). Otherwise return None ("no_sql").
    """
    if not text:
        return None
    sql_blocks = _SQL_BLOCK.findall(text)
    if sql_blocks:
        return sql_blocks[-1].strip() or None
    for block in reversed(_ANY_BLOCK.findall(text)):
        candidate = block.strip()
        if _STARTS_SQL.match(candidate):
            return candidate or None
    return None


def _text_of(content: list[Any]) -> str:
    """Join the text of every text block in an assistant message."""
    parts = [block.text for block in content if getattr(block, "type", None) == "text"]
    return "\n".join(parts)


def _content_text(content: list[Any]) -> str:
    """Join the text of MCP content blocks returned by a tool call."""
    parts = [block.text for block in content if getattr(block, "text", None) is not None]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# SQL execution for scoring (no row cap, long timeout)
# --------------------------------------------------------------------------- #
@dataclass
class ExecResult:
    status: str  # "ok" | "error" | "skipped"
    rows: list[tuple[Any, ...]] | None
    row_count: int | None
    error: str | None


SKIPPED = ExecResult("skipped", None, None, None)


async def execute_for_scoring(
    db_id: str, sql: str, timeout_s: int = SCORING_TIMEOUT_S
) -> ExecResult:
    """Run SQL as qg_reader with no row cap and a long timeout, for scoring.

    Parallel gather is disabled so floating-point SUMs are added in the same order
    every time; otherwise a REAL aggregate can wobble in its low digits between
    runs and straddle the scorer's rounding boundary (non-reproducible scores).
    """
    try:
        pool = await get_pool(db_id)
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_s)}s'")
            await conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            records = await conn.fetch(sql)
        rows = [tuple(record) for record in records]
        return ExecResult("ok", rows, len(rows), None)
    except (asyncpg.PostgresError, OSError) as exc:
        return ExecResult("error", None, None, str(exc))


class RunStopped(RuntimeError):
    """Base for conditions that must halt the whole run, not score the question.

    Subclasses carry a ``headline`` so the operator sees what kind of failure it
    was (a database outage vs. a provider limit) alongside the resume command.
    """

    headline = "RUN STOPPED"


class InfrastructureError(RunStopped):
    """The database looks unreachable — stop the run rather than score the model.

    Raised when a database-connectivity failure (not a normal SQL error) shows up
    in a tool result, in gold/predicted execution, or in a health check, so an
    outage is never mistaken for the model getting the answer wrong.
    """

    headline = "INFRASTRUCTURE FAILURE — stopping the run."


class ProviderError(RunStopped):
    """A model-provider limit or hard error — stop the run, don't score the model.

    Covers daily rate limits, request-too-large/context-length, auth failures,
    repeated 5xx responses, and exhausting the per-minute rate-limit retries.
    """

    headline = "PROVIDER LIMIT/ERROR — stopping the run."


# Substrings (matched case-insensitively) that mean "the database is unreachable",
# as opposed to a normal SQL error from the model's query.
_INFRA_SIGNATURES = (
    "connection refused",
    "refused the network connection",  # Windows WinError 1225
    "connection reset",
    "reset by peer",
    "forcibly closed",  # Windows WinError 10054
    "cannot connect",
    "could not connect",
    "connection closed",
    "connection was closed",  # asyncpg ConnectionDoesNotExistError
    "server closed the connection",
    "connection does not exist",
    "underlying connection is closed",  # asyncpg InterfaceError
    "database unreachable",  # queryguard.executor's "unavailable" tool error prefix
)


def is_infra_error(message: str | None) -> bool:
    """True if ``message`` looks like a database-unreachable error."""
    if not message:
        return False
    low = message.lower()
    return any(signature in low for signature in _INFRA_SIGNATURES)


def _raise_if_infra(message: str | None, context: str) -> None:
    if is_infra_error(message):
        raise InfrastructureError(f"{context}: {message}")


async def _db_health_check(db_id: str, execute_sql: Callable[..., Any]) -> None:
    """Raise InfrastructureError unless a trivial SELECT 1 succeeds on ``db_id``."""
    health = await execute_sql(db_id, "SELECT 1")
    if health.status != "ok":
        raise InfrastructureError(f"health check (SELECT 1) failed for {db_id!r}: {health.error}")


def decide_correctness(
    result_status: str, gold: ExecResult, pred: ExecResult, scoring_module: Any
) -> tuple[str, bool | None, str | None]:
    """Apply the accuracy rule, returning ``(result_status, correct, scoring_error)``.

    ``correct`` is null in exactly three situations: the gold SQL did not run
    (``result_status`` becomes ``"gold_error"``), the scorer is not yet
    implemented (NotImplementedError), or the scorer blew up on this record
    (``result_status`` becomes ``"scoring_error"`` and ``scoring_error`` holds the
    message). A scoring failure must never discard the paid API result, so the
    caller still writes the record and the run continues.

    ``is_correct`` must return *exactly* ``True`` or ``False``; any other value is
    a scorer bug, raised here as ``TypeError`` and handled like any other scorer
    exception.
    """
    # Gold SQL must run, or there is nothing trustworthy to compare against.
    if gold.status != "ok":
        return "gold_error", None, None
    # The model produced no SQL at all -> wrong.
    if result_status == "no_sql":
        return result_status, False, None
    # The model's SQL failed to execute (error or timeout) -> wrong.
    if pred.status != "ok":
        return result_status, False, None
    # Both ran: let the scorer judge.
    try:
        verdict = scoring_module.is_correct(pred.rows, gold.rows)
        if verdict is not True and verdict is not False:
            raise TypeError(
                f"is_correct must return exactly True or False, got "
                f"{type(verdict).__name__}: {verdict!r}"
            )
    except NotImplementedError:
        # Scorer not written yet: leave the verdict open, keep the paid result.
        return result_status, None, None
    except Exception as exc:  # noqa: BLE001 - never lose a paid result to a scorer bug
        return "scoring_error", None, f"{type(exc).__name__}: {exc}"
    return result_status, verdict, None


# --------------------------------------------------------------------------- #
# Token accounting and cost
# --------------------------------------------------------------------------- #
def _zero_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def _accumulate_usage(totals: dict[str, int], usage: Any) -> None:
    totals["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
    totals["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
    totals["cache_read_tokens"] += getattr(usage, "cache_read_input_tokens", 0) or 0
    totals["cache_write_tokens"] += getattr(usage, "cache_creation_input_tokens", 0) or 0


def _anthropic_prompt_tokens(usage: Any) -> int:
    """Total prompt tokens for one Anthropic request (fresh input + cache read/write)."""
    return (
        (getattr(usage, "input_tokens", 0) or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    )


def _zero_openai_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }


def _accumulate_openai_usage(totals: dict[str, int], usage: Any) -> None:
    totals["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
    totals["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details is not None:
        totals["cached_tokens"] += getattr(prompt_details, "cached_tokens", 0) or 0
    # Reasoning tokens are part of completion_tokens for billing, but we also keep
    # them separately so reasoning cost is visible per record.
    completion_details = getattr(usage, "completion_tokens_details", None)
    if completion_details is not None:
        totals["reasoning_tokens"] += getattr(completion_details, "reasoning_tokens", 0) or 0


def _reasoning_effort_for(model: str) -> str | None:
    """The reasoning_effort to send for a model, or None if it takes none.

    Only the openai/gpt-oss family accepts reasoning_effort; its value comes from
    EVAL_REASONING_EFFORT (default "medium").
    """
    if model.startswith("openai/gpt-oss"):
        return os.environ.get("EVAL_REASONING_EFFORT", "medium")
    return None


def _normalized_tokens(provider: str, usage: dict[str, int]) -> dict[str, int]:
    """Map a provider's usage dict onto the input/output/cache keys estimate_cost wants."""
    if provider == GROQ:
        return {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_read_tokens": usage.get("cached_tokens", 0),
            "cache_write_tokens": 0,
        }
    return usage


def _rate_limit_from_headers(headers: Any) -> dict[str, Any] | None:
    """Pull the limit and remaining requests/tokens from the x-ratelimit-* headers."""
    if headers is None:
        return None
    return {
        "limit_requests": headers.get("x-ratelimit-limit-requests"),
        "remaining_requests": headers.get("x-ratelimit-remaining-requests"),
        "limit_tokens": headers.get("x-ratelimit-limit-tokens"),
        "remaining_tokens": headers.get("x-ratelimit-remaining-tokens"),
    }


def load_pricing(path: str | Path = PRICING_PATH) -> dict[str, dict[str, Any]]:
    with open(path, "rb") as fh:
        return tomllib.load(fh).get("prices", {})


def estimate_cost(model: str, usage: dict[str, int], prices: dict[str, Any]) -> float | None:
    """Cost in USD from per-million-token prices, or None if any price is blank."""
    rates_raw = prices.get(model)
    if not rates_raw:
        return None
    rates: dict[str, float] = {}
    for key in ("input", "output", "cache_read", "cache_write"):
        value = rates_raw.get(key, "")
        if value == "" or value is None:
            return None
        rates[key] = float(value)
    cost = (
        usage["input_tokens"] * rates["input"]
        + usage["output_tokens"] * rates["output"]
        + usage["cache_read_tokens"] * rates["cache_read"]
        + usage["cache_write_tokens"] * rates["cache_write"]
    ) / 1_000_000
    return round(cost, 6)


# --------------------------------------------------------------------------- #
# Anthropic call with retry
# --------------------------------------------------------------------------- #
async def create_with_retry(client: Any, *, max_retries: int = API_MAX_RETRIES, **kwargs: Any):
    """Call ``messages.create`` with exponential backoff on transient API errors."""
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            return await client.messages.create(**kwargs)
        except (anthropic.APIStatusError, anthropic.APIConnectionError):
            if attempt >= max_retries:
                raise
            await asyncio.sleep(delay + random.random() * 0.5)
            delay *= 2


# --------------------------------------------------------------------------- #
# The per-question agent loop
# --------------------------------------------------------------------------- #
async def _call_mcp_tool(client: Client, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    result = await client.call_tool(name, arguments or {}, raise_on_error=False)
    return _content_text(result.content), bool(result.is_error)


def _dump_blocks(content: list[Any]) -> list[dict[str, Any]]:
    """Serialise assistant content blocks to plain dicts (for transcript + replay)."""
    dumped = []
    for block in content:
        if hasattr(block, "model_dump"):
            dumped.append(block.model_dump(exclude_none=True))
        else:
            dumped.append(block)
    return dumped


@dataclass
class LoopResult:
    """What a provider agent-loop produces for one question."""

    final_text: str | None
    transcript: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]
    tool_call_count: int
    rate_limit: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    max_request_prompt_tokens: int = 0
    final_turn_failed: bool = False
    failed_generation: str | None = None


async def _run_anthropic_loop(
    *,
    client: Any,
    model: str,
    system_text: str,
    question_text: str,
    mcp_client: Client,
    qid: int,
    db_id: str,
    max_tool_calls: int,
) -> LoopResult:
    """Anthropic agent loop: prompt caching on the system prompt and tools."""
    system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    messages: list[dict[str, Any]] = [{"role": "user", "content": question_text}]
    tools = to_anthropic_tools(await mcp_client.list_tools())
    usage = _zero_usage()
    tool_calls: list[dict[str, Any]] = []
    tool_call_count = 0
    final_text: str | None = None
    max_request_prompt_tokens = 0

    while True:
        resp = await create_with_retry(
            client,
            model=model,
            max_tokens=MAX_TOKENS,
            temperature=0,
            system=system,
            messages=messages,
            tools=tools,
        )
        _accumulate_usage(usage, resp.usage)
        max_request_prompt_tokens = max(
            max_request_prompt_tokens, _anthropic_prompt_tokens(resp.usage)
        )
        messages.append({"role": "assistant", "content": _dump_blocks(resp.content)})

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            final_text = _text_of(resp.content)
            break

        tool_results = []
        for tool_use in tool_uses:
            result_text, is_error = await _call_mcp_tool(mcp_client, tool_use.name, tool_use.input)
            if is_error:
                _raise_if_infra(result_text, f"tool {tool_use.name!r} for question {qid} ({db_id})")
            tool_call_count += 1
            tool_calls.append(
                {
                    "step": tool_call_count,
                    "name": tool_use.name,
                    "arguments": tool_use.input,
                    "result": result_text,
                    "is_error": is_error,
                }
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result_text,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})

        if tool_call_count >= max_tool_calls:
            messages.append({"role": "user", "content": FINAL_PROMPT})
            final_resp = await create_with_retry(
                client,
                model=model,
                max_tokens=MAX_TOKENS,
                temperature=0,
                system=system,
                messages=messages,
                tools=tools,
                tool_choice={"type": "none"},
            )
            _accumulate_usage(usage, final_resp.usage)
            max_request_prompt_tokens = max(
                max_request_prompt_tokens, _anthropic_prompt_tokens(final_resp.usage)
            )
            messages.append({"role": "assistant", "content": _dump_blocks(final_resp.content)})
            final_text = _text_of(final_resp.content)
            break

    return LoopResult(
        final_text,
        messages,
        tool_calls,
        usage,
        tool_call_count,
        max_request_prompt_tokens=max_request_prompt_tokens,
    )


def _provider_error_message(exc: Exception) -> str:
    """Prefer the provider's structured error message, falling back to str(exc)."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(exc)


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error_body(exc: Exception) -> dict[str, Any] | None:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    # The openai SDK already unwraps the JSON envelope's "error" object into
    # exc.body (AsyncOpenAI._make_status_error: ``data = body.get("error", body)``),
    # so body is the inner error dict here. Tolerate a full {"error": {...}} too.
    inner = body.get("error")
    return inner if isinstance(inner, dict) else body


def _is_tool_use_failed(exc: Exception) -> bool:
    """True for Groq's 400 'tool choice is none, but model called a tool' error."""
    if getattr(exc, "status_code", None) != 400:
        return False
    err = _error_body(exc)
    if err is None:
        return False
    message = str(err.get("message") or "").lower()
    return err.get("code") == "tool_use_failed" or "tool choice is none" in message


def _failed_generation(exc: Exception) -> str | None:
    """The 'failed_generation' text Groq returns with a tool_use_failed error, if any."""
    err = _error_body(exc)
    if err is None:
        return None
    value = err.get("failed_generation")
    return str(value) if value is not None else None


async def _groq_create(client: Any, *, context: str, **kwargs: Any) -> tuple[Any, Any]:
    """One Groq (OpenAI-compatible) call, returning (completion, response headers).

    Per-minute 429s are waited out (retry-after, else exponential backoff), up to
    ``MAX_RATE_LIMIT_WAITS``. Daily limits, request-too-large/context-length, auth
    errors, and repeated 5xx/connection errors raise :class:`ProviderError` to stop
    the whole run. An unrecognised error is re-raised unchanged.
    """
    waits = 0
    server_errors = 0
    backoff = 1.0
    while True:
        try:
            raw = await client.chat.completions.with_raw_response.create(**kwargs)
            return raw.parse(), raw.headers
        except openai.APIError as exc:
            status = getattr(exc, "status_code", None)
            message = _provider_error_message(exc)
            low = message.lower()

            if status == 429:
                # Only an explicit per-day quota is a hard daily stop. Groq's 429
                # text always ends "Upgrade to Dev Tier today", so matching a bare
                # "day"/"daily" wrongly stopped a per-minute (TPM) limit whose text
                # merely contained "today" (q207, 2026-10-09). Match TPD/RPD only.
                if any(
                    sign in low for sign in ("tokens per day", "requests per day", "(tpd)", "(rpd)")
                ):
                    raise ProviderError(f"{context}: daily rate limit: {message}") from exc
                waits += 1
                if waits > MAX_RATE_LIMIT_WAITS:
                    raise ProviderError(
                        f"{context}: still rate limited after {MAX_RATE_LIMIT_WAITS} waits: {message}"
                    ) from exc
                wait = _retry_after_seconds(exc)
                if wait is None:
                    wait = backoff
                    backoff = min(backoff * 2, 60.0)
                print(
                    f"WARNING: rate limited (429) for {context}; waiting {wait:.1f}s then "
                    f"retrying ({waits}/{MAX_RATE_LIMIT_WAITS})...",
                    file=sys.stderr,
                )
                await asyncio.sleep(wait)
                continue

            if status in (401, 403):
                raise ProviderError(f"{context}: auth error ({status}): {message}") from exc

            too_large = status == 413 or any(
                s in low for s in ("context length", "request too large", "too large", "reduce the")
            )
            if too_large:
                raise ProviderError(f"{context}: request too large ({status}): {message}") from exc

            transient = (status is not None and 500 <= status < 600) or isinstance(
                exc, openai.APIConnectionError
            )
            if transient:
                server_errors += 1
                if server_errors >= MAX_SERVER_ERRORS:
                    raise ProviderError(
                        f"{context}: repeated server/connection errors ({status}): {message}"
                    ) from exc
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            raise  # an unexpected API error (e.g. a plain 400) — surface it


async def _run_groq_loop(
    *,
    client: Any,
    model: str,
    system_text: str,
    question_text: str,
    mcp_client: Client,
    qid: int,
    db_id: str,
    max_tool_calls: int,
) -> LoopResult:
    """Groq agent loop over the OpenAI-compatible API (no prompt caching)."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": question_text},
    ]
    tools = to_openai_tools(await mcp_client.list_tools())
    usage = _zero_openai_usage()
    tool_calls: list[dict[str, Any]] = []
    tool_call_count = 0
    final_text: str | None = None
    rate_limit: dict[str, Any] | None = None
    max_request_prompt_tokens = 0
    final_turn_failed = False
    failed_generation: str | None = None
    context = f"question {qid} ({db_id})"
    reasoning_effort = _reasoning_effort_for(model)
    # reasoning_effort is only sent when the model accepts it.
    extra: dict[str, Any] = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}

    while True:
        completion, headers = await _groq_create(
            client,
            context=context,
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0,
            **extra,
        )
        rate_limit = _rate_limit_from_headers(headers) or rate_limit
        _accumulate_openai_usage(usage, getattr(completion, "usage", None))
        max_request_prompt_tokens = max(
            max_request_prompt_tokens,
            getattr(getattr(completion, "usage", None), "prompt_tokens", 0) or 0,
        )
        message = completion.choices[0].message
        calls = list(getattr(message, "tool_calls", None) or [])

        assistant_entry: dict[str, Any] = {"role": "assistant", "content": message.content}
        if calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in calls
            ]
        messages.append(assistant_entry)

        if not calls:
            final_text = message.content
            break

        for call in calls:
            name = call.function.name
            raw_args = call.function.arguments
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as exc:
                # Malformed tool arguments: tell the model, count it, never crash.
                result_text = f"Invalid JSON in tool arguments: {exc}. Received: {raw_args!r}"
                is_error = True
                recorded_args: Any = raw_args
            else:
                recorded_args = arguments
                result_text, is_error = await _call_mcp_tool(mcp_client, name, arguments)
                if is_error:
                    _raise_if_infra(result_text, f"tool {name!r} for {context}")
            tool_call_count += 1
            tool_calls.append(
                {
                    "step": tool_call_count,
                    "name": name,
                    "arguments": recorded_args,
                    "result": result_text,
                    "is_error": is_error,
                }
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})

        if tool_call_count >= max_tool_calls:
            messages.append({"role": "user", "content": FINAL_PROMPT})
            completion = None
            for attempt in range(1, FINAL_TURN_ATTEMPTS + 1):
                try:
                    completion, headers = await _groq_create(
                        client,
                        context=context,
                        model=model,
                        messages=messages,
                        tools=tools,
                        tool_choice="none",
                        temperature=0,
                        **extra,
                    )
                    break
                except openai.APIError as exc:
                    # Only retry gpt-oss's "tool choice is none, but model called a
                    # tool" (400 tool_use_failed); anything else is a real error.
                    if not _is_tool_use_failed(exc):
                        raise
                    failed_generation = _failed_generation(exc) or failed_generation
                    print(
                        f"WARNING: final turn tool_use_failed for {context} "
                        f"(attempt {attempt}/{FINAL_TURN_ATTEMPTS})",
                        file=sys.stderr,
                    )
            else:
                # All attempts failed: give up on this question's final answer.
                final_turn_failed = True

            if final_turn_failed:
                final_text = None
                break

            failed_generation = None  # a clean final answer arrived; discard any earlier one
            rate_limit = _rate_limit_from_headers(headers) or rate_limit
            _accumulate_openai_usage(usage, getattr(completion, "usage", None))
            max_request_prompt_tokens = max(
                max_request_prompt_tokens,
                getattr(getattr(completion, "usage", None), "prompt_tokens", 0) or 0,
            )
            final_message = completion.choices[0].message
            messages.append({"role": "assistant", "content": final_message.content})
            final_text = final_message.content
            break

    return LoopResult(
        final_text,
        messages,
        tool_calls,
        usage,
        tool_call_count,
        rate_limit,
        reasoning_effort=reasoning_effort,
        max_request_prompt_tokens=max_request_prompt_tokens,
        final_turn_failed=final_turn_failed,
        failed_generation=failed_generation,
    )


async def run_question(
    question: dict[str, Any],
    *,
    condition: str,
    model: str,
    client: Any,
    server: Any,
    prices: dict[str, Any],
    provider: str = ANTHROPIC,
    execute_sql: Callable[..., Any] = execute_for_scoring,
    scoring_module: Any = scoring,
    max_tool_calls: int = MAX_TOOL_CALLS,
) -> dict[str, Any]:
    """Run one question end to end and return its result record.

    Raises :class:`RunStopped` (InfrastructureError or ProviderError) if the
    database looks unreachable or the model provider hit a hard limit, so the
    caller can stop the run instead of recording a model failure.
    """
    db_id = question["db_id"]
    qid = question["question_id"]
    system_text = system_prompt(db_id, question.get("evidence", ""), condition)

    loop_fn = _run_groq_loop if provider == GROQ else _run_anthropic_loop
    start = time.perf_counter()
    async with Client(server) as mcp_client:
        loop = await loop_fn(
            client=client,
            model=model,
            system_text=system_text,
            question_text=question["question"],
            mcp_client=mcp_client,
            qid=qid,
            db_id=db_id,
            max_tool_calls=max_tool_calls,
        )
    latency = time.perf_counter() - start

    if loop.final_turn_failed:
        # The model never produced a clean final answer (gpt-oss kept calling a
        # tool on the forced final turn). Wrong, but not scored.
        final_sql = None
        result_status = "final_turn_failed"
    else:
        final_sql = extract_last_sql(loop.final_text)
        result_status = "answered" if final_sql else "no_sql"

    gold = await execute_sql(db_id, question["SQL"])
    pred = await execute_sql(db_id, final_sql) if final_sql else SKIPPED

    # A connectivity failure in gold or predicted execution is an outage, not a
    # wrong answer — stop before scoring or recording it.
    _raise_if_infra(gold.error, f"gold execution for question {qid} ({db_id})")
    _raise_if_infra(pred.error, f"predicted execution for question {qid} ({db_id})")

    if loop.final_turn_failed:
        correct: bool | None = False
        scoring_error = None
    else:
        result_status, correct, scoring_error = decide_correctness(
            result_status, gold, pred, scoring_module
        )
        if gold.status != "ok":
            print(
                f"WARNING: gold SQL failed for question {qid} ({db_id}): {gold.error}",
                file=sys.stderr,
            )
        if scoring_error is not None:
            print(
                f"WARNING: scoring failed for question {qid} ({db_id}): {scoring_error}",
                file=sys.stderr,
            )

    # Final guard: confirm the database is still reachable before this record is
    # committed, so a mid-question outage never masquerades as a graded result.
    await _db_health_check(db_id, execute_sql)

    return {
        "question_id": qid,
        "db_id": db_id,
        "difficulty": question["difficulty"],
        "provider": provider,
        "condition": condition,
        "model": model,
        "prompt_version": prompt_version(),
        "transcript": loop.transcript,
        "tool_calls": loop.tool_calls,
        "final_sql": final_sql,
        "result": result_status,
        "failed_generation": loop.failed_generation if loop.final_turn_failed else None,
        "gold": {"status": gold.status, "error": gold.error, "row_count": gold.row_count},
        "pred": {"status": pred.status, "error": pred.error, "row_count": pred.row_count},
        "correct": correct,
        "scoring_error": scoring_error,
        "usage": loop.usage,
        "rate_limit": loop.rate_limit,
        "reasoning_effort": loop.reasoning_effort,
        "max_request_prompt_tokens": loop.max_request_prompt_tokens,
        "latency_seconds": round(latency, 3),
        "tool_call_count": loop.tool_call_count,
        "cost_usd": estimate_cost(model, _normalized_tokens(provider, loop.usage), prices),
    }


# --------------------------------------------------------------------------- #
# Fake Anthropic client for --dry-run and tests
# --------------------------------------------------------------------------- #
@dataclass
class FakeBlock:
    """Minimal stand-in for an Anthropic content block."""

    _data: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return dict(self._data)


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str
    usage: FakeUsage = field(default_factory=FakeUsage)


def text_block(text: str) -> FakeBlock:
    return FakeBlock({"type": "text", "text": text})


def tool_use_block(block_id: str, name: str, tool_input: dict[str, Any]) -> FakeBlock:
    return FakeBlock({"type": "tool_use", "id": block_id, "name": name, "input": tool_input})


Responder = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None, Any], FakeResponse]


class _FakeMessages:
    def __init__(self, responder: Responder) -> None:
        self._responder = responder

    async def create(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        **_kwargs: Any,
    ) -> FakeResponse:
        return self._responder(messages, tools, tool_choice)


class FakeAnthropic:
    """Drop-in async Anthropic client whose replies come from ``responder``."""

    def __init__(self, responder: Responder) -> None:
        self.messages = _FakeMessages(responder)


def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if (
            message.get("role") == "user"
            and isinstance(content, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        ):
            return True
    return False


def _is_forced_final(tool_choice: Any) -> bool:
    return isinstance(tool_choice, dict) and tool_choice.get("type") == "none"


def default_dry_run_responder(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, tool_choice: Any
) -> FakeResponse:
    """Canned behaviour for --dry-run: one tool call, then a final SQL answer."""
    if _is_forced_final(tool_choice) or _has_tool_result(messages):
        return FakeResponse(
            [text_block("Here is the query.\n\n```sql\nSELECT 1 AS demo;\n```")],
            "end_turn",
        )
    names = {t["name"] for t in (tools or [])}
    if "list_databases" in names:
        call = tool_use_block("call_1", "list_databases", {})
    else:
        call = tool_use_block("call_1", "run_sql", {"sql": "SELECT 1 AS demo"})
    return FakeResponse([text_block("Let me explore first."), call], "tool_use")


# --------------------------------------------------------------------------- #
# Fake OpenAI/Groq client for --dry-run and tests
# --------------------------------------------------------------------------- #
@dataclass
class FakeOpenAIFunction:
    name: str
    arguments: str


@dataclass
class FakeOpenAIToolCall:
    id: str
    function: FakeOpenAIFunction
    type: str = "function"


@dataclass
class FakeOpenAIMessage:
    content: str | None
    tool_calls: list[FakeOpenAIToolCall] | None = None


@dataclass
class FakeOpenAIChoice:
    message: FakeOpenAIMessage


@dataclass
class FakeOpenAIPromptDetails:
    cached_tokens: int = 0


@dataclass
class FakeOpenAICompletionDetails:
    reasoning_tokens: int = 0


@dataclass
class FakeOpenAIUsage:
    prompt_tokens: int = 50
    completion_tokens: int = 10
    prompt_tokens_details: FakeOpenAIPromptDetails | None = None
    completion_tokens_details: FakeOpenAICompletionDetails | None = None


@dataclass
class FakeChatCompletion:
    choices: list[FakeOpenAIChoice]
    usage: FakeOpenAIUsage = field(default_factory=FakeOpenAIUsage)


def openai_text_completion(text: str, usage: FakeOpenAIUsage | None = None) -> FakeChatCompletion:
    return FakeChatCompletion(
        [FakeOpenAIChoice(FakeOpenAIMessage(content=text))], usage or FakeOpenAIUsage()
    )


def openai_tool_completion(
    calls: list[tuple[str, str, str]], usage: FakeOpenAIUsage | None = None
) -> FakeChatCompletion:
    """Build a tool-call completion from (id, name, arguments-json-string) tuples."""
    tool_calls = [
        FakeOpenAIToolCall(id=cid, function=FakeOpenAIFunction(name, args))
        for cid, name, args in calls
    ]
    return FakeChatCompletion(
        [FakeOpenAIChoice(FakeOpenAIMessage(content=None, tool_calls=tool_calls))],
        usage or FakeOpenAIUsage(),
    )


@dataclass
class _FakeOpenAIRawResponse:
    _parsed: Any
    headers: dict[str, Any]

    def parse(self) -> Any:
        return self._parsed


OpenAIResponder = Callable[
    [list[dict[str, Any]], list[dict[str, Any]] | None, Any], FakeChatCompletion
]


class _FakeOpenAIRawCompletions:
    def __init__(
        self, responder: OpenAIResponder, headers: dict[str, Any], requests: list[dict[str, Any]]
    ) -> None:
        self._responder = responder
        self._headers = headers
        self._requests = requests

    async def create(self, **kwargs: Any) -> _FakeOpenAIRawResponse:
        self._requests.append(kwargs)  # record what was sent (e.g. reasoning_effort)
        completion = self._responder(
            kwargs.get("messages"), kwargs.get("tools"), kwargs.get("tool_choice")
        )
        return _FakeOpenAIRawResponse(completion, self._headers)


class _FakeOpenAICompletions:
    def __init__(
        self, responder: OpenAIResponder, headers: dict[str, Any], requests: list[dict[str, Any]]
    ) -> None:
        self.with_raw_response = _FakeOpenAIRawCompletions(responder, headers, requests)


class _FakeOpenAIChat:
    def __init__(
        self, responder: OpenAIResponder, headers: dict[str, Any], requests: list[dict[str, Any]]
    ) -> None:
        self.completions = _FakeOpenAICompletions(responder, headers, requests)


class FakeOpenAI:
    """Drop-in async OpenAI/Groq client whose replies come from ``responder``.

    ``headers`` are returned on every successful call (for x-ratelimit-* tests).
    Every call's kwargs are recorded in ``requests`` so tests can assert on what
    was sent (e.g. reasoning_effort). The responder may raise openai exceptions to
    exercise the limit handling.
    """

    def __init__(
        self, responder: OpenAIResponder, *, headers: dict[str, Any] | None = None
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.chat = _FakeOpenAIChat(responder, headers or {}, self.requests)


def _openai_has_tool_result(messages: list[dict[str, Any]]) -> bool:
    return any(m.get("role") == "tool" for m in messages)


def default_dry_run_openai_responder(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, tool_choice: Any
) -> FakeChatCompletion:
    """Canned Groq behaviour for --dry-run: one tool call, then a final SQL answer."""
    if tool_choice == "none" or _openai_has_tool_result(messages):
        return openai_text_completion("Here is the query.\n\n```sql\nSELECT 1 AS demo;\n```")
    names = {t["function"]["name"] for t in (tools or [])}
    if "list_databases" in names:
        return openai_tool_completion([("call_1", "list_databases", "{}")])
    return openai_tool_completion([("call_1", "run_sql", '{"sql": "SELECT 1 AS demo"}')])


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _build_client(provider: str, dry_run: bool) -> Any:
    """Construct the model-provider client (or its fake) for a run."""
    if dry_run:
        if provider == GROQ:
            return FakeOpenAI(default_dry_run_openai_responder)
        return FakeAnthropic(default_dry_run_responder)
    if provider == GROQ:
        return openai.AsyncOpenAI(base_url=GROQ_BASE_URL, api_key=os.environ["GROQ_API_KEY"])
    return anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _append_line(path: Path, line: str) -> None:
    """Blocking append of one line; call via ``asyncio.to_thread`` from async code."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def _resume_ids(path: Path) -> set[int]:
    done: set[int] = set()
    if not path.exists():
        return done
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                done.add(json.loads(line)["question_id"])
    return done


def _ensure_resume_prompt_version(path: Path, current_version: str) -> None:
    """Refuse to resume a run file whose records use a different/missing prompt_version."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record_version = json.loads(line).get("prompt_version")
            if record_version != current_version:
                raise SystemExit(
                    f"Refusing to resume {path}: its records have prompt_version "
                    f"{record_version!r}, but the current prompt is {current_version!r}. "
                    "The prompt template changed — start a new run file (drop --resume)."
                )


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _safe_model_name(model: str) -> str:
    """Make a model id safe for a filename (e.g. replace '/' with '_')."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", model)


def _resume_command(
    condition: str,
    sample_path: str | Path,
    n: int | None,
    concurrency: int,
    dry_run: bool,
    out_path: Path,
) -> str:
    """The exact command that continues this run, skipping questions already done."""
    parts = [
        "uv run python -m eval.harness",
        f"--condition {condition}",
        f"--sample {sample_path}",
    ]
    if n is not None:
        parts.append(f"--n {n}")
    if concurrency != 2:
        parts.append(f"--concurrency {concurrency}")
    if dry_run:
        parts.append("--dry-run")
    parts.append(f"--resume {out_path}")
    return " ".join(parts)


async def run_eval(
    *,
    condition: str,
    sample_path: str | Path,
    n: int | None = None,
    concurrency: int = 2,
    resume: str | None = None,
    dry_run: bool = False,
    execute_sql: Callable[..., Any] = execute_for_scoring,
) -> Path:
    """Run the evaluation for one condition and return the output JSONL path.

    If the database becomes unreachable mid-run, stop immediately: the failing
    question's record is not written, and the exact ``--resume`` command is
    printed so the run can continue once the database is back.
    """
    questions = load_questions()
    by_id = {q["question_id"]: q for q in questions}
    # Read-only: a sample passed with --sample is never created or modified here.
    sample_ids = load_sample(sample_path)
    if n is not None:
        sample_ids = sample_ids[:n]

    provider = os.environ.get("EVAL_PROVIDER", ANTHROPIC).lower()
    if provider not in (ANTHROPIC, GROQ):
        raise SystemExit(f"Unknown EVAL_PROVIDER {provider!r}; use 'anthropic' or 'groq'.")

    model = os.environ.get("EVAL_MODEL") or ("dry-run" if dry_run else None)
    if not model:
        raise SystemExit("EVAL_MODEL is not set. Add it to .env (see .env.example).")
    key_var = "GROQ_API_KEY" if provider == GROQ else "ANTHROPIC_API_KEY"
    if not dry_run and not os.environ.get(key_var):
        raise SystemExit(f"{key_var} is not set. Add it to .env (see .env.example).")

    version = prompt_version()
    if resume:
        out_path = Path(resume)
        _ensure_resume_prompt_version(out_path, version)
        done = _resume_ids(out_path)
    else:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = RUNS_DIR / (
            f"{condition}_{provider}_{_safe_model_name(model)}_{version}_{_timestamp()}.jsonl"
        )
        done = set()

    todo = [by_id[qid] for qid in sample_ids if qid in by_id and qid not in done]

    client = _build_client(provider, dry_run)
    prices = load_pricing()
    main_server = build_server() if condition == "main" else None

    errors_path = out_path.with_suffix(".errors.jsonl")
    errors: list[dict[str, Any]] = []  # unexpected per-question errors (not RunStopped)

    semaphore = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()
    errors_lock = asyncio.Lock()
    stop = asyncio.Event()  # set as soon as the database looks unreachable

    async def worker(question: dict[str, Any]) -> None:
        if stop.is_set():
            return
        async with semaphore:
            try:
                server = (
                    main_server
                    if condition == "main"
                    else await build_baseline_server(question["db_id"])
                )
                record = await run_question(
                    question,
                    condition=condition,
                    model=model,
                    client=client,
                    server=server,
                    prices=prices,
                    provider=provider,
                    execute_sql=execute_sql,
                )
            except RunStopped:
                stop.set()
                raise
            except Exception as exc:
                if is_infra_error(str(exc)):
                    stop.set()
                    raise InfrastructureError(str(exc)) from exc
                # Any other unexpected error: record it, do not write a record for
                # this question, and keep going with the rest of the run.
                entry = {
                    "question_id": question["question_id"],
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": "".join(traceback.format_exception(exc)),
                }
                async with errors_lock:
                    errors.append(entry)
                    await asyncio.to_thread(_append_line, errors_path, json.dumps(entry) + "\n")
                print(
                    f"WARNING: unexpected error on question {question['question_id']}: "
                    f"{type(exc).__name__}: {exc} — recorded to {errors_path.name}, continuing.",
                    file=sys.stderr,
                )
                return
        # If another worker already hit an outage, do not write this record.
        if stop.is_set():
            return
        line = json.dumps(record) + "\n"
        async with write_lock:
            await asyncio.to_thread(_append_line, out_path, line)

    results = await asyncio.gather(*(worker(q) for q in todo), return_exceptions=True)

    resume_cmd = _resume_command(condition, sample_path, n, concurrency, dry_run, out_path)
    bar = "=" * 70

    # Always report unexpected per-question errors, even if the run also stopped.
    if errors:
        errored_ids = [e["question_id"] for e in errors]
        print(
            f"\n{bar}\n"
            f"{len(errors)} question(s) errored and were NOT recorded: {errored_ids}\n"
            f"Details (type, message, traceback) in: {errors_path}\n"
            "Re-run the errored (and any unstarted) questions with:\n"
            f"    {resume_cmd}\n"
            f"{bar}",
            file=sys.stderr,
        )

    stopped = next((r for r in results if isinstance(r, RunStopped)), None)
    if stopped is not None:
        print(
            f"\n{bar}\n"
            f"{stopped.headline}\n"
            f"{stopped}\n\n"
            "The remaining questions were NOT scored as model failures and no record "
            "was written for the failing question. Records already written are kept.\n"
            "Resume once it is resolved with:\n"
            f"    {resume_cmd}\n"
            f"{bar}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    # Surface any truly-unexpected BaseException (per-question errors are handled above).
    for result in results:
        if isinstance(result, BaseException):
            raise result

    if errors:
        raise SystemExit(1)  # the run finished, but some questions errored
    return out_path


def _read_records(path: Path) -> list[dict[str, Any]]:
    """Blocking read of a JSONL run file into a list of records."""
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _write_records(path: Path, records: list[dict[str, Any]]) -> None:
    """Blocking write of records as JSONL; call via ``asyncio.to_thread``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(record) + "\n" for record in records)


async def rescore_file(
    run_path: str | Path,
    *,
    execute_sql: Callable[..., Any] = execute_for_scoring,
    scoring_module: Any = scoring,
) -> Path:
    """Re-score an existing run file. No API calls.

    For every record, re-run the gold SQL (looked up by question_id) and the
    stored ``final_sql`` as qg_reader, apply the same accuracy rule as a live run,
    and write the updated records to ``<name>_rescored.jsonl`` next to the original.
    Only ``gold``, ``pred``, ``result`` and ``correct`` change.
    """
    run_path = Path(run_path)
    records = await asyncio.to_thread(_read_records, run_path)
    gold_sql_by_id = {q["question_id"]: q["SQL"] for q in load_questions()}

    for record in records:
        db_id = record["db_id"]
        gold_sql = gold_sql_by_id.get(record["question_id"])
        if gold_sql is None:
            print(
                f"WARNING: question {record['question_id']} is not in the dataset; "
                "leaving its record unchanged.",
                file=sys.stderr,
            )
            continue

        final_sql = record.get("final_sql")
        result_status = "answered" if final_sql else "no_sql"
        gold = await execute_sql(db_id, gold_sql)
        pred = await execute_sql(db_id, final_sql) if final_sql else SKIPPED

        # An outage during rescore must stop the whole thing, not overwrite the
        # record with a bogus False verdict. Nothing is written in that case.
        try:
            _raise_if_infra(
                gold.error, f"gold execution for question {record['question_id']} ({db_id})"
            )
            _raise_if_infra(
                pred.error, f"predicted execution for question {record['question_id']} ({db_id})"
            )
            await _db_health_check(db_id, execute_sql)
        except InfrastructureError as exc:
            bar = "=" * 70
            print(
                f"\n{bar}\n"
                "INFRASTRUCTURE FAILURE — stopping the rescore.\n"
                f"{exc}\n\n"
                "The database appears unreachable, so no rescored file was written "
                "(the original run file is untouched). Re-run once it is back with:\n"
                f"    uv run python -m eval.harness --rescore {run_path}\n"
                f"{bar}",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc

        result_status, correct, scoring_error = decide_correctness(
            result_status, gold, pred, scoring_module
        )
        if gold.status != "ok":
            print(
                f"WARNING: gold SQL failed for question {record['question_id']} "
                f"({db_id}): {gold.error}",
                file=sys.stderr,
            )
        if scoring_error is not None:
            print(
                f"WARNING: scoring failed for question {record['question_id']} "
                f"({db_id}): {scoring_error}",
                file=sys.stderr,
            )

        record["gold"] = {"status": gold.status, "error": gold.error, "row_count": gold.row_count}
        record["pred"] = {"status": pred.status, "error": pred.error, "row_count": pred.row_count}
        record["result"] = result_status
        record["correct"] = correct
        record["scoring_error"] = scoring_error

    out_path = run_path.with_name(f"{run_path.stem}_rescored{run_path.suffix}")
    await asyncio.to_thread(_write_records, out_path, records)
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QueryGuard evaluation harness.")
    # --condition and --sample are required for a live/dry run, but not for --rescore.
    parser.add_argument("--condition", choices=["baseline", "main"])
    parser.add_argument("--sample", help="Path to the sample JSON file.")
    parser.add_argument(
        "--n", type=int, default=None, help="Only run the first N sampled questions."
    )
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--resume", default=None, help="Append to this run file, skipping done ids."
    )
    parser.add_argument("--dry-run", action="store_true", help="Use a fake client; no API calls.")
    parser.add_argument(
        "--rescore",
        default=None,
        help="Re-score an existing run file (no API calls); ignores --condition/--sample.",
    )
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> Path:
    try:
        if args.rescore:
            return await rescore_file(args.rescore)
        if not args.condition or not args.sample:
            raise SystemExit("--condition and --sample are required unless --rescore is given.")
        return await run_eval(
            condition=args.condition,
            sample_path=args.sample,
            n=args.n,
            concurrency=args.concurrency,
            resume=args.resume,
            dry_run=args.dry_run,
        )
    finally:
        await close_all()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    out_path = asyncio.run(_amain(args))
    print(f"Wrote results to {out_path}")


if __name__ == "__main__":
    main()
