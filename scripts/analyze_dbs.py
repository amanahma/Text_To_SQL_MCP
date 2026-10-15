"""Run ANALYZE on every BIRD database so planner statistics (pg_stats) exist.

The schema tools read column statistics — null fraction, distinct estimate, and
most-common values — from ``pg_stats``, which PostgreSQL only populates when a
table has been ANALYZEd. A freshly loaded/split database has none, so this script
runs ``ANALYZE`` once per database. It is safe to re-run at any time.

ANALYZE needs table ownership, so it runs as the ``postgres`` superuser via psql
inside the Docker container (the same mechanism as the other setup scripts); the
read-only ``qg_reader`` role cannot ANALYZE.

Usage (PowerShell):
    uv run python scripts/analyze_dbs.py
"""

from __future__ import annotations

import sys

from _dockerutil import PROJECT_ROOT, psql_capture
from dotenv import dotenv_values

ENV_PATH = PROJECT_ROOT / ".env"


def databases() -> list[str]:
    raw = dotenv_values(ENV_PATH).get("BIRD_DBS")
    if not raw:
        sys.exit(f"BIRD_DBS is not set in {ENV_PATH}. Copy .env.example to .env and fill it in.")
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if not names:
        sys.exit(f"BIRD_DBS is empty in {ENV_PATH}.")
    return names


def main() -> None:
    names = databases()
    for database in names:
        # ANALYZE; (no table list) collects statistics for every table in the db.
        psql_capture("ANALYZE;", db=database)
        print(f"ANALYZE done on '{database}'.")
    print(f"Done: statistics refreshed on {len(names)} databases.")


if __name__ == "__main__":
    main()
