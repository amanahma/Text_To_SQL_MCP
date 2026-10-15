"""Phase 6 go/no-go readiness check. Prints PASS/FAIL per gate; exits 1 if any FAIL.

Gates:
  1. git repo exists and the tree is clean (prints commit hash and the phase6-eval tag)
  2. sample_150 sha256 matches both the sample file and PREREGISTRATION.md
  3. prompt_version a16401d6 in the dev run files and PREREGISTRATION
  4. baseline and main agree on model / reasoning_effort, and neither exceeds the tool cap
  5. GROQ_API_KEY is set (prints only "set" and its length, never the key)
  6. prices.json is filled (numeric input and output)
  7. prices.json has a numeric daily_token_limit
  INFO. expected tokens for a 150-question run = 150 x the dev_10 per-question mean, per
        condition, shown against daily_token_limit with the number of days each needs.

The token estimate uses the dev_10 run files (the sample_150 runs do not exist yet).
INFO lines never block readiness; only PASS/FAIL gates do.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL = PROJECT_ROOT / "eval"
SAMPLE = EVAL / "sample_150.json"
PREREG = EVAL / "PREREGISTRATION.md"
PRICES = EVAL / "prices.json"
DEV_BASE = EVAL / "runs" / "baseline_groq_openai_gpt-oss-120b_a16401d6_20261004T183548Z.jsonl"
DEV_MAIN = EVAL / "runs" / "main_groq_openai_gpt-oss-120b_a16401d6_20261006T175358Z.jsonl"
EXPECTED_PROMPT = "a16401d6"
TOOL_CAP = 10
# Groq free-tier daily token limit (one account). Used only for the key-switch INFO
# line; falls back to this when prices.json has no positive daily_token_limit.
FREE_TIER_DAILY = 200_000


def _load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _load_prices() -> dict:
    return json.loads(PRICES.read_text(encoding="utf-8")) if PRICES.exists() else {}


def _git(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
        )
        return r.returncode, (r.stdout or r.stderr).strip()
    except FileNotFoundError:
        return 127, "git not found"


def _tokens_per_q(recs: list[dict]) -> float:
    if not recs:
        return 0.0
    totals = []
    for r in recs:
        u = r.get("usage") or {}
        totals.append((u.get("prompt_tokens", 0) or 0) + (u.get("completion_tokens", 0) or 0))
    return sum(totals) / len(recs)


def _cached_recorded(recs: list[dict]) -> bool:
    """True if any record's usage carries a cached_tokens field."""
    return any("cached_tokens" in (r.get("usage") or {}) for r in recs)


def _counted_tokens_per_q(recs: list[dict]) -> float:
    """Mean per-question counted tokens = prompt - cached + completion.

    Cached prompt tokens are subtracted because a cache hit is not re-charged
    against the daily budget; when cached_tokens is absent it is treated as 0.
    """
    if not recs:
        return 0.0
    totals = []
    for r in recs:
        u = r.get("usage") or {}
        prompt = u.get("prompt_tokens", 0) or 0
        cached = u.get("cached_tokens", 0) or 0
        completion = u.get("completion_tokens", 0) or 0
        totals.append(prompt - cached + completion)
    return sum(totals) / len(recs)


def check_git() -> tuple[bool, str]:
    code, _ = _git("rev-parse", "--is-inside-work-tree")
    if code != 0:
        return False, "not a git repository (run git init, add, commit, and tag phase6-eval)"
    _, head = _git("rev-parse", "HEAD")
    _, status = _git("status", "--porcelain")
    _, tag = _git("tag", "--list", "phase6-eval")
    clean = status == ""
    detail = f"commit={head[:12]} tag={'phase6-eval' if tag.strip() else '(missing)'} clean={clean}"
    return clean and bool(tag.strip()), detail


def check_sample_hash() -> tuple[bool, str]:
    doc = json.loads(SAMPLE.read_text(encoding="utf-8"))
    ids = sorted(doc["question_ids"])
    computed = hashlib.sha256(",".join(str(i) for i in ids).encode()).hexdigest()
    in_file = doc.get("sha256")
    in_prereg = computed in PREREG.read_text(encoding="utf-8")
    ok = computed == in_file and in_prereg
    return ok, f"computed={computed[:12]} file_match={computed == in_file} prereg_match={in_prereg}"


def check_prompt_version(base: list[dict], main: list[dict]) -> tuple[bool, str]:
    vals = {r.get("prompt_version") for r in base} | {r.get("prompt_version") for r in main}
    in_prereg = EXPECTED_PROMPT in PREREG.read_text(encoding="utf-8")
    ok = vals == {EXPECTED_PROMPT} and in_prereg
    return ok, f"run_files={vals} prereg_mentions={in_prereg}"


def check_conditions(base: list[dict], main: list[dict]) -> tuple[bool, str]:
    def one(recs, key):
        return {r.get(key) for r in recs}

    model_ok = one(base, "model") == one(main, "model") and len(one(base, "model")) == 1
    reason_ok = (
        one(base, "reasoning_effort") == one(main, "reasoning_effort")
        and len(one(base, "reasoning_effort")) == 1
    )
    cap_ok = all((r.get("tool_call_count") or 0) <= TOOL_CAP for r in base + main)
    ok = model_ok and reason_ok and cap_ok
    return (
        ok,
        f"model={sorted(one(base, 'model'))} reasoning={sorted(one(base, 'reasoning_effort'))} within_cap={cap_ok}",
    )


def check_groq_key() -> tuple[bool, str]:
    import queryguard.db  # noqa: F401  (import loads .env into the environment)

    key = os.environ.get("GROQ_API_KEY")
    if key:
        return True, f"set (len={len(key)})"
    return False, "not set"


def check_prices(prices: dict) -> tuple[bool, str]:
    if not PRICES.exists():
        return False, "prices.json missing"
    ok = isinstance(prices.get("input"), (int, float)) and isinstance(
        prices.get("output"), (int, float)
    )
    return ok, f"input={prices.get('input')} output={prices.get('output')}"


def check_daily_limit(prices: dict) -> tuple[bool, str]:
    limit = prices.get("daily_token_limit")
    ok = isinstance(limit, (int, float)) and not isinstance(limit, bool)
    return ok, (f"daily_token_limit={limit}" if ok else f"not set (daily_token_limit={limit})")


def expected_tokens_info(base: list[dict], main: list[dict], prices: dict) -> str:
    """INFO line: 150-run token estimate per condition vs the daily limit, with days needed."""
    b = _tokens_per_q(base) * 150
    m = _tokens_per_q(main) * 150
    limit = prices.get("daily_token_limit")
    if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
        return (
            f"baseline~{b:,.0f} ({math.ceil(b / limit)} day(s))  "
            f"main~{m:,.0f} ({math.ceil(m / limit)} day(s))  "
            f"at daily_token_limit={limit:,.0f}"
        )
    return f"baseline~{b:,.0f}  main~{m:,.0f}  (daily_token_limit not set; days unknown)"


def key_switches_info(base: list[dict], main: list[dict], prices: dict) -> str:
    """INFO line: expected account/key switches per condition on the Groq free tier.

    Uses counted tokens (prompt - cached + completion) x 150 against the free-tier
    daily token limit (prices.json daily_token_limit when positive, else the
    FREE_TIER_DAILY default). One account carries at most ``limit`` counted tokens,
    so accounts = ceil(total / limit) and switches = accounts - 1.
    """
    raw = prices.get("daily_token_limit")
    limit = (
        float(raw)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0
        else float(FREE_TIER_DAILY)
    )

    def one(recs: list[dict]) -> str:
        total = _counted_tokens_per_q(recs) * 150
        accounts = math.ceil(total / limit) if total > 0 else 0
        switches = max(0, accounts - 1)
        return f"~{total:,.0f} -> {switches} switch(es) ({accounts} account(s))"

    recorded = _cached_recorded(base) and _cached_recorded(main)
    return (
        f"baseline {one(base)}  main {one(main)}  "
        f"at daily_token_limit={limit:,.0f}  cached_recorded={recorded}"
    )


def main() -> int:
    base = _load(DEV_BASE) if DEV_BASE.exists() else []
    main_recs = _load(DEV_MAIN) if DEV_MAIN.exists() else []
    prices = _load_prices()

    gates = [
        ("git repo clean + tagged", check_git()),
        ("sample_150 sha256 matches", check_sample_hash()),
        ("prompt_version a16401d6", check_prompt_version(base, main_recs)),
        ("conditions agree (model/reasoning/cap)", check_conditions(base, main_recs)),
        ("GROQ_API_KEY", check_groq_key()),
        ("prices.json filled", check_prices(prices)),
        ("prices.json daily_token_limit", check_daily_limit(prices)),
    ]
    all_ok = True
    for name, (ok, detail) in gates:
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print(
        f"[INFO] expected tokens (150 x dev_10 mean): {expected_tokens_info(base, main_recs, prices)}"
    )
    print(
        f"[INFO] expected key switches (150 x dev_10 counted mean, prompt-cached+completion): "
        f"{key_switches_info(base, main_recs, prices)}"
    )
    print(f"\n{'READY' if all_ok else 'NOT READY'} for the Phase 6 run.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
