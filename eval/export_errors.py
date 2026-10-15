"""Export a blinded error table for manual labeling (Phase 6 error analysis).

One row per (question, condition) that was wrong or no_sql in that condition. The
condition name is replaced by an opaque blind_id; the blind_id -> condition key is
written to a separate file you open only after labeling. Rows are shuffled with a
fixed seed. Only *computed* flags are filled; the A/B/C/D category columns are left
blank for you.

Gold and predicted SQL are executed read-only as qg_reader to compute the flags
(no API calls, no row cap change; a short timeout). This is analysis, not a harness
run.

Usage (PowerShell):
    uv run python -m eval.export_errors --baseline <base.jsonl> --main <main.jsonl> \
        --sample eval/sample_150.json --out eval/errors_blinded.csv --key eval/errors_key.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import uuid
from pathlib import Path

import asyncpg

from queryguard.db import close_all, get_pool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "data" / "mini_dev_postgresql.json"
SCORE_TIMEOUT = "30s"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_sample_ids(path: Path) -> list[int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["question_ids"] if isinstance(data, dict) else data


async def run_sql(db_id: str, sql: str) -> tuple[int, list[tuple], str | None]:
    """Return (n_columns, rows, error) running sql read-only as qg_reader."""
    try:
        pool = await get_pool(db_id)
        async with pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = '{SCORE_TIMEOUT}'")
            await conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            stmt = await conn.prepare(sql)
            ncols = len(stmt.get_attributes())
            records = await stmt.fetch()
        return ncols, [tuple(r) for r in records], None
    except (asyncpg.PostgresError, OSError) as exc:
        return 0, [], str(exc).splitlines()[0]


def _is_empty(rows: list[tuple]) -> bool:
    return len(rows) == 0 or all(all(v is None for v in r) for r in rows)


def _canon(v: object) -> str:
    return "NULL" if v is None else str(v)


def _canon_round(v: object) -> str:
    if isinstance(v, bool) or v is None:
        return _canon(v)
    try:
        return str(round(float(v), 2))
    except (TypeError, ValueError):
        return _canon(v)


def _sorted_rows(rows: list[tuple], fn) -> list[tuple]:
    return sorted(tuple(fn(v) for v in row) for row in rows)


def _sorted_rows_colblind(rows: list[tuple], fn) -> list[tuple]:
    return sorted(tuple(sorted(fn(v) for v in row)) for row in rows)


def compute_flags(gold: list[tuple], gcols: int, pred: list[tuple], pcols: int) -> dict:
    same_sort = gcols == pcols and _sorted_rows(gold, _canon) == _sorted_rows(pred, _canon)
    same_round = gcols == pcols and _sorted_rows(gold, _canon_round) == _sorted_rows(
        pred, _canon_round
    )
    same_colblind = gcols == pcols and _sorted_rows_colblind(gold, _canon) == _sorted_rows_colblind(
        pred, _canon
    )
    return {
        "same_after_sorting_rows": same_sort,
        "same_after_rounding": same_round,
        "same_values_different_column_order": same_colblind,
        "pred_empty": _is_empty(pred),
        "gold_empty": _is_empty(gold),
    }


async def build_rows(base: list[dict], main: list[dict], sample_ids: list[int]) -> list[dict]:
    questions = {q["question_id"]: q for q in json.loads(DATA.read_text(encoding="utf-8"))}
    want = set(sample_ids)
    gold_cache: dict[int, tuple[int, list[tuple]]] = {}
    rows: list[dict] = []

    for condition, recs in (("baseline", base), ("main", main)):
        for r in recs:
            qid = r["question_id"]
            if qid not in want:
                continue
            if r.get("correct") is True:  # keep only wrong or no_sql
                continue
            q = questions[qid]
            if qid not in gold_cache:
                gc, gr, _ = await run_sql(q["db_id"], q["SQL"])
                gold_cache[qid] = (gc, gr)
            gcols, gold = gold_cache[qid]

            pred_sql = r.get("final_sql")
            if pred_sql:
                pcols, pred, perr = await run_sql(q["db_id"], pred_sql)
            else:
                pcols, pred, perr = 0, [], "no_sql"

            flags = compute_flags(gold, gcols, pred, pcols)
            rows.append(
                {
                    "blind_id": uuid.uuid4().hex[:12],
                    "question_id": qid,
                    "_condition": condition,  # removed from the blinded CSV
                    "db_id": q["db_id"],
                    "difficulty": q["difficulty"],
                    "result": r.get("result"),
                    "question": q["question"],
                    "evidence": q.get("evidence", ""),
                    "gold_sql": q["SQL"],
                    "predicted_sql": pred_sql or "",
                    "gold_rows": len(gold),
                    "gold_cols": gcols,
                    "pred_rows": len(pred),
                    "pred_cols": pcols,
                    "pred_error": perr or "",
                    **{k: ("TRUE" if v else "FALSE") for k, v in flags.items()},
                    "A_truly_wrong": "",
                    "B_wrong_shape": "",
                    "C_value_format": "",
                    "D_gold_doubtful": "",
                }
            )
    return rows


BLIND_FIELDS = [
    "blind_id",
    "question_id",
    "db_id",
    "difficulty",
    "result",
    "question",
    "evidence",
    "gold_sql",
    "predicted_sql",
    "gold_rows",
    "gold_cols",
    "pred_rows",
    "pred_cols",
    "pred_error",
    "same_after_sorting_rows",
    "same_after_rounding",
    "same_values_different_column_order",
    "pred_empty",
    "gold_empty",
    "A_truly_wrong",
    "B_wrong_shape",
    "C_value_format",
    "D_gold_doubtful",
]


async def collect_rows(args: argparse.Namespace) -> list[dict]:
    """All DB work: build the error rows, then close pools. Returns shuffled rows."""
    base = load_jsonl(Path(args.baseline))
    main_recs = load_jsonl(Path(args.main))
    sample_ids = load_sample_ids(Path(args.sample))
    try:
        rows = await build_rows(base, main_recs, sample_ids)
    finally:
        await close_all()
    random.Random(args.seed).shuffle(rows)
    return rows


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Blinded Phase 6 error export.")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--main", required=True)
    ap.add_argument("--sample", required=True)
    ap.add_argument("--out", default="eval/errors_blinded.csv")
    ap.add_argument("--key", default="eval/errors_key.csv")
    ap.add_argument("--seed", type=int, default=6150)
    args = ap.parse_args(argv)

    rows = asyncio.run(collect_rows(args))  # file writing happens below, off the event loop

    _write_csv(Path(args.out), BLIND_FIELDS, rows)
    key_rows = [
        {"blind_id": r["blind_id"], "question_id": r["question_id"], "condition": r["_condition"]}
        for r in rows
    ]
    _write_csv(Path(args.key), ["blind_id", "question_id", "condition"], key_rows)

    from collections import Counter

    print(f"wrote {len(rows)} blinded rows -> {args.out}")
    print(f"wrote key -> {args.key} (open only after labeling)")
    print("rows per condition (in key):", dict(Counter(r["_condition"] for r in rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
