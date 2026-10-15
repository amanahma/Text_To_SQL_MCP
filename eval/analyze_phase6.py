"""Phase 6 analysis: baseline vs main execution accuracy on a frozen sample.

Standard library only (math.comb for the exact McNemar test; no scipy). Reads two
run files (one per condition), the sample file, the tags file and an optional
prices.json, runs preregistered integrity checks, and prints a markdown report:
the 2x2 agreement table, each accuracy with a Wilson 95% interval, the paired
difference with a paired-bootstrap interval, the exact two-sided McNemar p, the
non-inferiority verdict, the preregistered secondary metrics, blocked main tool
calls, and correct answers where gold and prediction are both empty.

No claim is computed here beyond what PREREGISTRATION.md fixes. Integrity failures
stop the analysis -- a broken or mismatched pair of runs is never scored.

Usage (PowerShell):
    uv run python -m eval.analyze_phase6 --baseline <base.jsonl> --main <main.jsonl> \
        --sample eval/sample_150.json --tags eval/tags.json --prices eval/prices.json
Add --allow-small to analyze a sample smaller than 150 (e.g. a dev dry run).
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from statistics import median

Z95 = 1.96  # standard-normal 95% two-sided; Wilson upper for 0/10 is 0.2775 at this z
BOOT_RESAMPLES = 10_000
BOOT_SEED = 1234
TOOL_CAP = 10  # MAX_TOOL_CALLS in eval/harness.py (not stored per record; checked as a bound)
PRIMARY_N = 150
MAIN_TOOLS = [
    "list_databases",
    "list_tables",
    "describe_table",
    "find_columns",
    "validate_sql",
    "execute_query",
]
# Substrings in an errors.jsonl message that mark a 400 tool_use_failed (the model
# called a tool when tool_choice was "none", or produced a malformed tool call).
# Preregistered rule: such a question is scored no_sql in that condition; a later
# resume record for the same id is ignored.
TOOL_USE_FAILED_SIGNS = (
    "tool_use_failed",
    "tool choice is none",
)

# Substrings that mean an outage rather than a model/SQL mistake (mirror of the harness).
INFRA_SIGNS = (
    "connection refused",
    "refused the network connection",
    "connection reset",
    "reset by peer",
    "forcibly closed",
    "cannot connect",
    "could not connect",
    "connection closed",
    "connection was closed",
    "server closed the connection",
    "connection does not exist",
    "database unreachable",
)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def load_sample_ids(path: Path) -> list[int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["question_ids"] if isinstance(data, dict) else data


def load_tags(path: Path | None) -> dict[str, list[int]]:
    if path is None or not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {tag: list(ids) for tag, ids in doc.get("ids_by_tag", {}).items()}


def errors_path_for(run_path: Path) -> Path:
    """The sibling ``*.errors.jsonl`` the harness writes next to a run file."""
    return run_path.with_suffix(".errors.jsonl")


def load_tool_use_failed_ids(errors_path: Path) -> set[int]:
    """question_ids whose errors.jsonl entry is a 400 tool_use_failed (empty if no file)."""
    if not errors_path.exists():
        return set()
    ids: set[int] = set()
    for line in errors_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        message = str(entry.get("message") or "").lower()
        if any(sign in message for sign in TOOL_USE_FAILED_SIGNS):
            ids.add(entry["question_id"])
    return ids


def _single_values(recs: list[dict]) -> dict:
    """The single value of model/reasoning_effort/prompt_version across ``recs``, if unique."""
    out: dict = {}
    for key in ("model", "reasoning_effort", "prompt_version"):
        vals = {r.get(key) for r in recs if r.get(key) is not None}
        if len(vals) == 1:
            out[key] = next(iter(vals))
    return out


def _meta_lookup(recs: list[dict]) -> dict[int, dict]:
    """Map question_id -> {difficulty, db_id} from whatever real records carry them."""
    meta: dict[int, dict] = {}
    for r in recs:
        qid = r.get("question_id")
        if qid is None:
            continue
        entry = meta.setdefault(qid, {})
        for field in ("difficulty", "db_id"):
            if r.get(field) is not None:
                entry.setdefault(field, r[field])
    return meta


def apply_tool_use_failed_rule(
    recs: list[dict],
    tuf_ids: set[int],
    sample_ids: list[int],
    condition: str,
    meta: dict[int, dict],
    template: dict,
) -> tuple[list[dict], list[int]]:
    """Score every in-sample tool_use_failed id as no_sql in ``condition``.

    Any existing run-file record for such an id is dropped (the resume record is
    ignored) and replaced with a synthetic ``no_sql`` / ``correct=False`` record
    that carries the run's model/prompt metadata so integrity stays single-valued.
    Returns the rewritten record list and the sorted list of ids the rule touched.
    """
    applied = sorted(i for i in tuf_ids if i in set(sample_ids))
    if not applied:
        return recs, applied
    drop = set(applied)
    kept = [r for r in recs if r.get("question_id") not in drop]
    for qid in applied:
        synthetic = {
            "question_id": qid,
            "condition": condition,
            "result": "no_sql",
            "correct": False,
            "final_sql": None,
            "tool_call_count": 0,
            "latency_seconds": 0,
            "usage": {},
            "tool_calls": [],
            "gold": {},
            "pred": {},
            "_tool_use_failed_no_sql": True,
        }
        synthetic.update(meta.get(qid, {}))  # difficulty / db_id when another record has them
        synthetic.update(template)  # model / reasoning_effort / prompt_version
        kept.append(synthetic)
    return kept, applied


# --------------------------------------------------------------------------- #
# Statistics (pure, unit-tested)
# --------------------------------------------------------------------------- #
def wilson_interval(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score 95% interval for k successes in n trials, as a (lo, hi) proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def mcnemar_exact_two_sided(b: int, c: int) -> float:
    """Exact two-sided McNemar p from the discordant counts b and c.

    Under H0 each discordant pair is a fair coin, so the smaller count follows
    Binomial(n=b+c, 0.5). p = 2 * P(X <= min(b, c)), capped at 1.0.
    """
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2 * tail / (2**n))


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0, 1]) of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def paired_bootstrap_diff(
    base: list[bool], main: list[bool], resamples: int = BOOT_RESAMPLES, seed: int = BOOT_SEED
) -> tuple[float, float, float]:
    """Point and 95% CI (in percentage points) for main-accuracy minus baseline-accuracy.

    Resamples the paired per-question outcomes with replacement; the pairing (same
    question index for both conditions) is preserved in every resample.
    """
    n = len(base)
    point = (sum(main) - sum(base)) / n * 100
    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(resamples):
        d = 0
        for _ in range(n):
            i = rng.randrange(n)
            d += int(main[i]) - int(base[i])
        diffs.append(d / n * 100)
    diffs.sort()
    return point, _percentile(diffs, 0.025), _percentile(diffs, 0.975)


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #
def _distinct(recs: list[dict], key: str) -> set:
    return {r.get(key) for r in recs}


def integrity_checks(
    base: list[dict], main: list[dict], sample_ids: list[int], allow_small: bool
) -> list[str]:
    failures: list[str] = []
    want = set(sample_ids)

    for label, recs in (("baseline", base), ("main", main)):
        ids = [r["question_id"] for r in recs]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            failures.append(f"{label}: ids scored more than once: {dupes}")
        if set(ids) != want:
            missing = sorted(want - set(ids))
            extra = sorted(set(ids) - want)
            failures.append(
                f"{label}: id set != sample ({len(ids)} recs); missing={missing} extra={extra}"
            )
        over = [r["question_id"] for r in recs if (r.get("tool_call_count") or 0) > TOOL_CAP]
        if over:
            failures.append(f"{label}: tool_call_count over the {TOOL_CAP} cap: {over}")
        for r in recs:
            for side in ("gold", "pred"):
                err = (r.get(side) or {}).get("error")
                if err and any(s in err.lower() for s in INFRA_SIGNS):
                    failures.append(
                        f"{label}: infrastructure-error record q{r['question_id']} ({side}): {err[:80]}"
                    )

    if not allow_small and len(want) != PRIMARY_N:
        failures.append(
            f"sample has {len(want)} ids, not the primary {PRIMARY_N} (pass --allow-small for a dry run)"
        )

    # Both conditions must share model / reasoning_effort / prompt_version.
    for key in ("model", "reasoning_effort", "prompt_version"):
        bvals, mvals = _distinct(base, key), _distinct(main, key)
        if len(bvals) != 1 or len(mvals) != 1:
            failures.append(f"{key}: not single-valued (baseline={bvals}, main={mvals})")
        elif bvals != mvals:
            failures.append(f"{key}: baseline {bvals} != main {mvals}")

    # Guard against mixing conditions: each file must be a single, correct condition.
    if _distinct(base, "condition") != {"baseline"}:
        failures.append(
            f"baseline file condition(s): {_distinct(base, 'condition')} (expected baseline)"
        )
    if _distinct(main, "condition") != {"main"}:
        failures.append(f"main file condition(s): {_distinct(main, 'condition')} (expected main)")
    return failures


# --------------------------------------------------------------------------- #
# Metric helpers
# --------------------------------------------------------------------------- #
def is_correct(r: dict) -> bool:
    return r.get("correct") is True


def is_no_sql(r: dict) -> bool:
    return r.get("result") == "no_sql"


def record_empty(side: dict | None) -> bool:
    """A result side counts as empty when it returned 0 rows (only-NULL is tracked via tags)."""
    return bool(side) and side.get("status") == "ok" and (side.get("row_count") or 0) == 0


def tokens(r: dict, field: str) -> int:
    return (r.get("usage") or {}).get(field, 0) or 0


def stat_block(values: list[float]) -> str:
    if not values:
        return "n/a"
    s = sorted(values)
    return f"mean={sum(s) / len(s):.1f} median={median(s):.1f} p90={_percentile(s, 0.90):.1f}"


def fmt_pct(k: int, n: int) -> str:
    lo, hi = wilson_interval(k, n)
    return f"{k}/{n} = {k / n * 100:.1f}% (Wilson 95%: {lo * 100:.1f}-{hi * 100:.1f}%)"


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def build_report(
    base: list[dict],
    main: list[dict],
    sample_ids: list[int],
    tags: dict[str, list[int]],
    prices: dict | None,
    margin: float,
) -> str:
    bmap = {r["question_id"]: r for r in base}
    mmap = {r["question_id"]: r for r in main}
    ids = list(sample_ids)
    n = len(ids)

    b_ok = [is_correct(bmap[i]) for i in ids]
    m_ok = [is_correct(mmap[i]) for i in ids]

    both_right = sum(1 for i in range(n) if b_ok[i] and m_ok[i])
    only_base = sum(1 for i in range(n) if b_ok[i] and not m_ok[i])
    only_main = sum(1 for i in range(n) if m_ok[i] and not b_ok[i])
    both_wrong = sum(1 for i in range(n) if not b_ok[i] and not m_ok[i])

    base_k, main_k = sum(b_ok), sum(m_ok)
    point, ci_lo, ci_hi = paired_bootstrap_diff(b_ok, m_ok)
    mcnemar_p = mcnemar_exact_two_sided(only_base, only_main)

    not_worse = ci_lo > -margin
    better = ci_lo > 0

    L: list[str] = []
    L.append("# Phase 6 analysis report\n")
    L.append(
        f"- sample: {n} questions  (primary N = {PRIMARY_N}{'' if n == PRIMARY_N else '  -- SMALL/dry run'})"
    )
    L.append(
        f"- model: {min(_distinct(base, 'model'))}  reasoning_effort: {min(_distinct(base, 'reasoning_effort'))}  prompt_version: {min(_distinct(base, 'prompt_version'))}"
    )
    L.append(f"- non-inferiority margin: {margin} points\n")

    b_tuf = sorted(i for i in ids if bmap[i].get("_tool_use_failed_no_sql"))
    m_tuf = sorted(i for i in ids if mmap[i].get("_tool_use_failed_no_sql"))
    L.append("## tool_use_failed -> no_sql (preregistered rule)")
    L.append(f"- baseline: {len(b_tuf)} {b_tuf}")
    L.append(f"- main:     {len(m_tuf)} {m_tuf}\n")

    L.append("## Primary: execution accuracy")
    L.append(f"- baseline: {fmt_pct(base_k, n)}")
    L.append(f"- main:     {fmt_pct(main_k, n)}\n")

    L.append("## 2x2 agreement (paired)")
    L.append("| | main right | main wrong |")
    L.append("|---|---|---|")
    L.append(f"| **baseline right** | {both_right} | {only_base} |")
    L.append(f"| **baseline wrong** | {only_main} | {both_wrong} |")
    L.append(
        f"\n- discordant pairs: only-baseline-right b={only_base}, only-main-right c={only_main}\n"
    )

    L.append("## Paired difference (main - baseline)")
    L.append(f"- point estimate: {point:+.1f} points")
    L.append(
        f"- paired bootstrap 95% CI: [{ci_lo:+.1f}, {ci_hi:+.1f}] points ({BOOT_RESAMPLES} resamples, seed {BOOT_SEED})"
    )
    L.append(f"- exact two-sided McNemar p: {mcnemar_p:.4f} (b={only_base}, c={only_main})\n")

    L.append("## Non-inferiority verdict (preregistered)")
    L.append(
        f"- 'main not worse than baseline' (CI lower > -{margin}): **{'YES' if not_worse else 'NO'}** (lower={ci_lo:+.1f})"
    )
    L.append(f"- 'main better than baseline' (whole CI > 0): **{'YES' if better else 'NO'}**")
    if not not_worse and not better:
        L.append("- -> inconclusive at this n; no directional claim.")
    L.append("")

    # ---------------- secondary ----------------
    L.append("## Secondary metrics (reported; no standalone claims)\n")

    def acc_over(subset_ids: list[int]) -> tuple[int, int, int, int]:
        bk = sum(1 for i in subset_ids if is_correct(bmap[i]))
        mk = sum(1 for i in subset_ids if is_correct(mmap[i]))
        return bk, mk, len(subset_ids), len(subset_ids)

    L.append("### Accuracy by difficulty")
    L.append("| difficulty | n | baseline | main |")
    L.append("|---|---|---|---|")
    for d in ("simple", "moderate", "challenging"):
        sub = [i for i in ids if bmap[i].get("difficulty") == d]
        if sub:
            bk, mk, _, _ = acc_over(sub)
            L.append(
                f"| {d} | {len(sub)} | {bk}/{len(sub)} ({bk / len(sub) * 100:.0f}%) | {mk}/{len(sub)} ({mk / len(sub) * 100:.0f}%) |"
            )

    L.append("\n### Accuracy by db_id")
    L.append("| db_id | n | baseline | main |")
    L.append("|---|---|---|---|")
    for db in sorted({bmap[i].get("db_id") for i in ids}, key=lambda d: (d is None, d or "")):
        sub = [i for i in ids if bmap[i].get("db_id") == db]
        bk, mk, _, _ = acc_over(sub)
        L.append(f"| {db} | {len(sub)} | {bk}/{len(sub)} | {mk}/{len(sub)} |")

    L.append("\n### Accuracy excluding each tag (one tag at a time)")
    L.append("| excluded tag | removed | n | baseline | main |")
    L.append("|---|---|---|---|---|")
    for tag in ("pii_masked", "gold_issue", "empty_gold"):
        tagged = set(tags.get(tag, []))
        sub = [i for i in ids if i not in tagged]
        removed = n - len(sub)
        if sub:
            bk, mk, _, _ = acc_over(sub)
            L.append(
                f"| {tag} | {removed} | {len(sub)} | {bk}/{len(sub)} ({bk / len(sub) * 100:.1f}%) | {mk}/{len(sub)} ({mk / len(sub) * 100:.1f}%) |"
            )

    b_nosql = sum(1 for i in ids if is_no_sql(bmap[i]))
    m_nosql = sum(1 for i in ids if is_no_sql(mmap[i]))
    L.append(f"\n### no_sql count\n- baseline: {b_nosql}   main: {m_nosql}")

    def answered_only(mp) -> str:
        answered = [i for i in ids if not is_no_sql(mp[i])]
        k = sum(1 for i in answered if is_correct(mp[i]))
        return f"{k}/{len(answered)} ({k / len(answered) * 100:.1f}%)" if answered else "n/a"

    L.append("\n### Answered-only accuracy (no_sql excluded from denominator)")
    L.append(f"- baseline: {answered_only(bmap)}   main: {answered_only(mmap)}")

    b_cap = [i for i in ids if (bmap[i].get("tool_call_count") or 0) >= TOOL_CAP]
    m_cap = [i for i in ids if (mmap[i].get("tool_call_count") or 0) >= TOOL_CAP]
    L.append(f"\n### Questions that hit MAX_TOOL_CALLS ({TOOL_CAP})")
    L.append(f"- baseline: {len(b_cap)} {b_cap}   main: {len(m_cap)} {m_cap}")

    L.append("\n### Tokens per question")
    for label, mp in (("baseline", bmap), ("main", mmap)):
        L.append(f"- {label}:")
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens"):
            L.append(f"    - {field}: {stat_block([tokens(mp[i], field) for i in ids])}")

    L.append("\n### Tool calls per question")
    L.append(f"- baseline: {stat_block([(bmap[i].get('tool_call_count') or 0) for i in ids])}")
    L.append(f"- main:     {stat_block([(mmap[i].get('tool_call_count') or 0) for i in ids])}")

    per_tool = {t: 0 for t in MAIN_TOOLS}
    other_tools: dict[str, int] = {}
    for i in ids:
        for c in mmap[i].get("tool_calls", []):
            if c["name"] in per_tool:
                per_tool[c["name"]] += 1
            else:
                other_tools[c["name"]] = other_tools.get(c["name"], 0) + 1
    L.append("\n### Main per-tool call counts (total over the sample)")
    for t in MAIN_TOOLS:
        L.append(f"- {t}: {per_tool[t]}")
    if other_tools:
        L.append(f"- (unexpected tools: {other_tools})")

    L.append("\n### Latency (seconds per question)")
    L.append(f"- baseline: {stat_block([(bmap[i].get('latency_seconds') or 0) for i in ids])}")
    L.append(f"- main:     {stat_block([(mmap[i].get('latency_seconds') or 0) for i in ids])}")

    # ---------------- cost ----------------
    L.append("\n### Dollar cost")
    L.append(_cost_section(ids, bmap, mmap, prices))

    # ---------------- blocked main tool calls ----------------
    L.append("\n## Main tool calls blocked by check_sql or the cost gate")
    blocked = []
    for i in ids:
        for c in mmap[i].get("tool_calls", []):
            if c.get("is_error"):
                res = c.get("result") or ""
                low = res.lower().lstrip()
                if low.startswith(("blocked:", "cost:")):
                    blocked.append((i, c["name"], res))
    if blocked:
        for qid, name, res in blocked:
            L.append(f"- q{qid} {name}: {res[:300]}")
    else:
        L.append("- none")

    # ---------------- both-empty correct ----------------
    L.append("\n## Correct answers where gold AND prediction are both empty")
    L.append("(gold empty = empty_gold tag OR gold.row_count==0; pred empty = pred.row_count==0)")
    empty_gold_ids = set(tags.get("empty_gold", []))
    any_both = False
    for label, mp in (("baseline", bmap), ("main", mmap)):
        hits = []
        for i in ids:
            r = mp[i]
            if not is_correct(r):
                continue
            gold = r.get("gold") or {}
            pred = r.get("pred") or {}
            gold_empty = i in empty_gold_ids or (gold.get("row_count") or 0) == 0
            pred_empty = (pred.get("row_count") or 0) == 0 and pred.get("status") == "ok"
            if gold_empty and pred_empty:
                hits.append(i)
        if hits:
            any_both = True
            L.append(f"- {label}: {hits}")
    if not any_both:
        L.append("- none")

    return "\n".join(L) + "\n"


def _cost_section(ids: list[int], bmap: dict, mmap: dict, prices: dict | None) -> str:
    """Cost from prices.json (per 1M tokens). Missing/blank prices => not computed."""
    if not prices:
        return "- cost: not computed (no prices.json)"
    inp = prices.get("input")
    out = prices.get("output")
    if not isinstance(inp, (int, float)) or not isinstance(out, (int, float)):
        return "- cost: not computed (prices.json not filled)"
    lines = []
    for label, mp in (("baseline", bmap), ("main", mmap)):
        p = sum(tokens(mp[i], "prompt_tokens") for i in ids)
        c = sum(tokens(mp[i], "completion_tokens") for i in ids)
        cost = (p * inp + c * out) / 1_000_000
        lines.append(
            f"- {label}: ${cost:.4f}  (prompt={p}, completion={c}; rates ${inp}/${out} per 1M)"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 6 baseline-vs-main analysis.")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--main", required=True)
    ap.add_argument("--sample", required=True)
    ap.add_argument("--tags", default=None)
    ap.add_argument("--prices", default=None)
    ap.add_argument("--margin", type=float, default=10.0)
    ap.add_argument("--allow-small", action="store_true")
    ap.add_argument("--out", default=None, help="Write the report here as well as stdout.")
    args = ap.parse_args(argv)

    base = load_jsonl(Path(args.baseline))
    main_recs = load_jsonl(Path(args.main))
    sample_ids = load_sample_ids(Path(args.sample))
    tags = load_tags(Path(args.tags)) if args.tags else {}
    prices = None
    if args.prices and Path(args.prices).exists():
        prices = json.loads(Path(args.prices).read_text(encoding="utf-8"))

    # Preregistered tool_use_failed rule: an in-sample id that the model failed with
    # a 400 tool_use_failed (logged to the sibling *.errors.jsonl) is scored no_sql
    # in that condition, whether or not it is missing from the run file. Do this
    # before the integrity check so such an id is not reported as a missing record.
    base_tuf = load_tool_use_failed_ids(errors_path_for(Path(args.baseline)))
    main_tuf = load_tool_use_failed_ids(errors_path_for(Path(args.main)))
    meta = _meta_lookup(base + main_recs)
    base_tmpl = _single_values(base)
    main_tmpl = _single_values(main_recs)
    for key in ("model", "reasoning_effort", "prompt_version"):
        if key not in base_tmpl and key in main_tmpl:
            base_tmpl[key] = main_tmpl[key]
        if key not in main_tmpl and key in base_tmpl:
            main_tmpl[key] = base_tmpl[key]
    base, base_applied = apply_tool_use_failed_rule(
        base, base_tuf, sample_ids, "baseline", meta, base_tmpl
    )
    main_recs, main_applied = apply_tool_use_failed_rule(
        main_recs, main_tuf, sample_ids, "main", meta, main_tmpl
    )
    print(
        f"tool_use_failed -> no_sql: baseline {len(base_applied)} {base_applied}, "
        f"main {len(main_applied)} {main_applied}"
    )

    failures = integrity_checks(base, main_recs, sample_ids, args.allow_small)
    if failures:
        print("INTEGRITY CHECK FAILED -- analysis aborted:")
        for f in failures:
            print(f"  - {f}")
        return 1

    report = build_report(base, main_recs, sample_ids, tags, prices, args.margin)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
