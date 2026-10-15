"""Row count per table in all 11 BIRD databases, saved as JSON.

Run before and after the attack set and diff the two files: any change in a row
count means an attack mutated data (it must not — the attacks are read-only
probes). Read-only; connects as qg_reader.

Usage (PowerShell):
    uv run python scripts/db_fingerprint.py --out logs/fingerprint_before.json
    uv run python scripts/db_fingerprint.py --out logs/fingerprint_after.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from queryguard.db import allowed_databases, close_all, get_pool

_TABLES_SQL = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'r' AND n.nspname = 'public' ORDER BY c.relname"
)


async def fingerprint() -> dict:
    out: dict[str, dict[str, int]] = {}
    try:
        for db_id in allowed_databases():
            pool = await get_pool(db_id)
            async with pool.acquire() as conn, conn.transaction(readonly=True):
                tables = [r["relname"] for r in await conn.fetch(_TABLES_SQL)]
                counts: dict[str, int] = {}
                for t in tables:
                    counts[t] = await conn.fetchval(f'SELECT count(*) FROM public."{t}"')
            out[db_id] = counts
    finally:
        await close_all()
    return out


async def main_async(args: argparse.Namespace) -> int:
    data = await fingerprint()
    doc = {"taken_at": datetime.now(UTC).isoformat(), "row_counts": data}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    total = sum(sum(t.values()) for t in data.values())
    print(
        f"wrote {args.out}: {len(data)} databases, {sum(len(t) for t in data.values())} tables, {total} total rows"
    )
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Row-count fingerprint of all BIRD databases.")
    ap.add_argument("--out", default="logs/fingerprint.json")
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
