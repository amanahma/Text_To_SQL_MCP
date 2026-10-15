"""Load the BIRD PostgreSQL dump into the ``bird`` database inside the Docker container.

The dump (data/BIRD_dev.sql) contains no CREATE DATABASE/\\connect, puts every table
in the ``public`` schema, and sets ``OWNER TO`` roles that must already exist. So this
loader:

  1. Ensures the ``bird`` database exists (creating it if needed).
  2. Pre-creates every role the dump references (as NOLOGIN) so the owner-setting
     statements succeed under ``psql -v ON_ERROR_STOP=1``. The dump is never edited.
  3. Loads the dump with psql running *inside* the container.

It is safe to re-run: if ``bird`` already has tables it exits, unless ``--force`` is
passed (which drops and recreates ``bird`` first).

Usage:
    uv run python scripts/load_bird.py [--force]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

from _dockerutil import COMPOSE_SERVICE, PROJECT_ROOT, compose_base, psql_capture

TARGET_DB = "bird"
DUMP_IN_CONTAINER = "/bird_data/BIRD_dev.sql"
DUMP_ON_HOST = PROJECT_ROOT / "data" / "BIRD_dev.sql"
LOG_PATH = PROJECT_ROOT / "logs" / "load_bird.log"

# Matches e.g. `ALTER TABLE public.account OWNER TO xiaolongli;`
OWNER_RE = re.compile(r'OWNER TO "?([A-Za-z_][\w$]*)"?;')


def ensure_container_running() -> None:
    """Exit with a helpful message if the db container is not reachable."""
    try:
        psql_capture("SELECT 1;")
    except Exception as exc:  # noqa: BLE001 - surface any failure as a clear hint
        sys.exit(
            "Cannot reach the database container. Start it first:\n"
            "    docker compose up -d\n"
            f"(underlying error: {exc})"
        )


def database_exists(name: str) -> bool:
    return psql_capture(f"SELECT 1 FROM pg_database WHERE datname = '{name}';") == "1"


def user_table_count(db: str) -> int:
    out = psql_capture(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema');",
        db=db,
    )
    return int(out or "0")


def discover_roles() -> set[str]:
    """Stream the dump's schema section and collect roles referenced via OWNER TO.

    All OWNER statements precede the first COPY (data) block, so we stop there and
    never read the ~955 MB of row data.
    """
    roles: set[str] = set()
    with DUMP_ON_HOST.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("COPY "):
                break
            match = OWNER_RE.search(line)
            if match:
                roles.add(match.group(1))
    return roles


def create_roles(roles: set[str]) -> None:
    for role in sorted(roles):
        # NOLOGIN: the role can own objects (so the dump loads) but cannot connect.
        sql = (
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') "
            f'THEN CREATE ROLE "{role}" NOLOGIN; END IF; END $$;'
        )
        psql_capture(sql)
        print(f"  role ready: {role} (NOLOGIN)")


def drop_database(name: str) -> None:
    psql_capture(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);')


def create_database(name: str) -> None:
    psql_capture(f'CREATE DATABASE "{name}";')


def load_dump() -> None:
    """Run psql inside the container to load the dump, teeing output to the log file."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = compose_base() + [
        "exec",
        "-T",
        COMPOSE_SERVICE,
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        TARGET_DB,
        "-f",
        DUMP_IN_CONTAINER,
    ]
    with LOG_PATH.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n\n")
        log.flush()
        result = subprocess.run(
            cmd, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, check=False
        )
    if result.returncode != 0:
        sys.exit(f"psql load failed (exit {result.returncode}). See {LOG_PATH} for details.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the BIRD dump into the bird database.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Drop and recreate bird before loading, even if it already has tables.",
    )
    args = parser.parse_args()

    if not DUMP_ON_HOST.is_file():
        sys.exit(f"Dump not found: {DUMP_ON_HOST}")

    ensure_container_running()

    if database_exists(TARGET_DB):
        existing = user_table_count(TARGET_DB)
        if existing > 0 and not args.force:
            print(
                f"'{TARGET_DB}' already has {existing} tables. "
                "Nothing to do (pass --force to drop and reload)."
            )
            return
        if args.force:
            print(f"--force: dropping and recreating '{TARGET_DB}'...")
            drop_database(TARGET_DB)
            create_database(TARGET_DB)
    else:
        print(f"Creating database '{TARGET_DB}'...")
        create_database(TARGET_DB)

    roles = discover_roles()
    print(f"Roles referenced by dump: {', '.join(sorted(roles)) or '(none)'}")
    create_roles(roles)

    print(f"Loading dump into '{TARGET_DB}' (this can take 10+ minutes)...")
    print(f"psql output -> {LOG_PATH}")
    start = time.perf_counter()
    load_dump()
    elapsed = time.perf_counter() - start

    tables = user_table_count(TARGET_DB)
    print(f"Done. Loaded {tables} tables in {elapsed:.1f}s ({elapsed / 60:.1f} min).")


if __name__ == "__main__":
    main()
