"""Split the monolithic ``bird`` database into one database per BIRD db_id.

Phase 0 loaded all 11 BIRD databases into a single ``bird`` database (one public
schema). Each BIRD question, though, is meant to run against only its own
database. This script creates one database per db_id and copies just that
db_id's tables out of ``bird``.

Which tables belong to a db_id is taken from the gold SQL in
data/mini_dev_postgresql.json: the set of tables its queries reference. (In
Phase 0 this was verified to be a clean partition of all 75 tables.)

For each db_id it runs, entirely inside the container::

    pg_dump --no-owner --no-privileges -t <table> ... -d bird | psql -d <db_id>

so the new tables are owned by ``postgres``. Each new database is verified
against ``bird`` (same table set, same exact row count per table). Only after
all 11 verify does it drop ``bird`` and the now-unused ``xiaolongli`` role.

Idempotent: databases that already verify are skipped; ``--force`` rebuilds them.

Usage:
    uv run python scripts/split_bird.py [--force]
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from collections import defaultdict

import sqlglot
from _dockerutil import COMPOSE_SERVICE, PROJECT_ROOT, compose_base, psql_capture
from sqlglot import exp

SOURCE_DB = "bird"
LEGACY_ROLE = "xiaolongli"
QUESTIONS_PATH = PROJECT_ROOT / "data" / "mini_dev_postgresql.json"


def tables_referenced(sql: str) -> set[str]:
    """Base table names referenced by a SQL statement, excluding CTE names."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:  # noqa: BLE001 - unparseable queries contribute nothing
        return set()
    if tree is None:
        return set()
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = {tbl.name.lower() for tbl in tree.find_all(exp.Table) if tbl.name}
    return tables - cte_names


def db_id_table_map() -> dict[str, set[str]]:
    """Map each db_id to the set of tables its gold SQL references."""
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    mapping: dict[str, set[str]] = defaultdict(set)
    for question in questions:
        mapping[question["db_id"]] |= tables_referenced(question["SQL"])
    return dict(mapping)


def ensure_container_running() -> None:
    try:
        psql_capture("SELECT 1;")
    except Exception as exc:  # noqa: BLE001 - surface as a clear hint
        sys.exit(
            "Cannot reach the database container. Start it first:\n"
            "    docker compose up -d\n"
            f"(underlying error: {exc})"
        )


def database_exists(name: str) -> bool:
    return psql_capture(f"SELECT 1 FROM pg_database WHERE datname = '{name}';") == "1"


def role_exists(name: str) -> bool:
    return psql_capture(f"SELECT 1 FROM pg_roles WHERE rolname = '{name}';") == "1"


def create_database(name: str) -> None:
    psql_capture(f'CREATE DATABASE "{name}";')


def drop_database(name: str) -> None:
    psql_capture(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);')


def drop_role(name: str) -> None:
    psql_capture(f'DROP ROLE IF EXISTS "{name}";')


def tables_in(db: str) -> set[str]:
    out = psql_capture(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';",
        db=db,
    )
    return {line for line in out.splitlines() if line}


def row_count(db: str, table: str) -> int:
    return int(psql_capture(f'SELECT count(*) FROM "{table}";', db=db))


def copy_tables(target_db: str, tables: set[str]) -> None:
    """Copy the given tables from bird into target_db via pg_dump | psql in the container."""
    table_flags = " ".join(f"-t {shlex.quote(table)}" for table in sorted(tables))
    # pipefail so a pg_dump failure fails the whole pipeline, not just psql's exit.
    inner = (
        "set -o pipefail; "
        f"pg_dump --no-owner --no-privileges -U postgres -d {shlex.quote(SOURCE_DB)} {table_flags} "
        f"| psql -v ON_ERROR_STOP=1 -U postgres -d {shlex.quote(target_db)}"
    )
    cmd = compose_base() + ["exec", "-T", COMPOSE_SERVICE, "bash", "-c", inner]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"copy into '{target_db}' failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )


def verify(db: str, expected: set[str], *, source_available: bool) -> tuple[bool, str]:
    """Check a target database has the expected tables (and matching row counts if bird exists)."""
    if not database_exists(db):
        return False, "database missing"
    present = tables_in(db)
    missing = expected - present
    if missing:
        return False, f"missing tables: {sorted(missing)}"
    if not source_available:
        return True, f"{len(present)} tables (bird already dropped; row counts not re-checked)"
    for table in sorted(expected):
        src = row_count(SOURCE_DB, table)
        dst = row_count(db, table)
        if src != dst:
            return False, f"row count mismatch for {table}: bird={src}, {db}={dst}"
    return True, f"{len(present)} tables, row counts match"


def main() -> None:
    parser = argparse.ArgumentParser(description="Split bird into one database per db_id.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild every per-db_id database even if it already verifies.",
    )
    args = parser.parse_args()

    if not QUESTIONS_PATH.is_file():
        sys.exit(f"Questions file not found: {QUESTIONS_PATH}")

    ensure_container_running()

    mapping = db_id_table_map()
    if not mapping:
        sys.exit("No db_ids found in the questions file.")
    print(f"Found {len(mapping)} db_ids covering {sum(len(t) for t in mapping.values())} tables.")

    source_available = database_exists(SOURCE_DB)
    if not source_available:
        print(f"Note: source database '{SOURCE_DB}' is not present (already split).")

    verified: dict[str, bool] = {}
    for db_id in sorted(mapping):
        expected = mapping[db_id]

        if not args.force and database_exists(db_id):
            ok, note = verify(db_id, expected, source_available=source_available)
            if ok:
                print(f"[skip] {db_id}: already verified ({note})")
                verified[db_id] = True
                continue
            print(f"[rebuild] {db_id}: does not verify ({note})")

        if not source_available:
            sys.exit(
                f"Cannot build '{db_id}': source '{SOURCE_DB}' is gone. "
                "Re-run scripts/load_bird.py first."
            )

        print(f"[build] {db_id}: copying {len(expected)} tables...")
        drop_database(db_id)
        create_database(db_id)
        copy_tables(db_id, expected)
        ok, note = verify(db_id, expected, source_available=True)
        if not ok:
            sys.exit(f"[FAIL] {db_id} did not verify: {note}")
        print(f"[ok] {db_id}: {note}")
        verified[db_id] = True

    if not (len(verified) == len(mapping) and all(verified.values())):
        sys.exit("Not all databases verified; leaving 'bird' in place.")

    if source_available:
        freed = int(psql_capture(f"SELECT pg_database_size('{SOURCE_DB}');"))
        print(
            f"\nAll {len(mapping)} databases verified. Dropping '{SOURCE_DB}' and role '{LEGACY_ROLE}'..."
        )
        drop_database(SOURCE_DB)
        if role_exists(LEGACY_ROLE):
            drop_role(LEGACY_ROLE)
        print(f"Dropped '{SOURCE_DB}' (freed {freed:,} bytes) and role '{LEGACY_ROLE}'.")
        print(f"FREED_BYTES={freed}")
    else:
        print(
            f"\nAll {len(mapping)} databases present and verified; '{SOURCE_DB}' was already dropped."
        )
        if role_exists(LEGACY_ROLE):
            drop_role(LEGACY_ROLE)
            print(f"Dropped lingering role '{LEGACY_ROLE}'.")


if __name__ == "__main__":
    main()
