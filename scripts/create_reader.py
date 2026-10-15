"""Create (or refresh) the read-only ``qg_reader`` role on every BIRD database.

Reads READER_USER / READER_PASSWORD / BIRD_DBS from .env, then runs
``scripts/create_reader.sql`` with psql inside the Docker container once per
database. Idempotent.

Usage:
    uv run python scripts/create_reader.py
"""

from __future__ import annotations

import subprocess
import sys

from _dockerutil import COMPOSE_SERVICE, PROJECT_ROOT, compose_base
from dotenv import dotenv_values

SQL_PATH = PROJECT_ROOT / "scripts" / "create_reader.sql"
ENV_PATH = PROJECT_ROOT / ".env"


def load_config() -> tuple[str, str, list[str]]:
    values = dotenv_values(ENV_PATH)
    user = values.get("READER_USER")
    password = values.get("READER_PASSWORD")
    dbs_raw = values.get("BIRD_DBS")
    if not user or not password or not dbs_raw:
        sys.exit(
            f"Missing READER_USER / READER_PASSWORD / BIRD_DBS in {ENV_PATH}. "
            "Copy .env.example to .env and fill it in."
        )
    databases = [name.strip() for name in dbs_raw.split(",") if name.strip()]
    if not databases:
        sys.exit(f"BIRD_DBS is empty in {ENV_PATH}.")
    return user, password, databases


def ensure_container_running() -> None:
    cmd = compose_base() + [
        "exec",
        "-T",
        COMPOSE_SERVICE,
        "psql",
        "-U",
        "postgres",
        "-tAc",
        "SELECT 1;",
    ]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(
            "Cannot reach the database container. Start it first:\n"
            "    docker compose up -d\n"
            f"(underlying error: {result.stderr.strip()})"
        )


def configure_reader(database: str, user: str, password: str, sql: str) -> None:
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
        database,
        "-v",
        f"reader_user={user}",
        "-v",
        f"reader_password={password}",
    ]
    result = subprocess.run(
        cmd, cwd=PROJECT_ROOT, input=sql, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        # Scrub the password from anything we print, defence-in-depth.
        sys.stderr.write(result.stdout.replace(password, "***"))
        sys.stderr.write(result.stderr.replace(password, "***"))
        sys.exit(f"create_reader.sql failed on '{database}' (exit {result.returncode}).")


def main() -> None:
    if not SQL_PATH.is_file():
        sys.exit(f"SQL script not found: {SQL_PATH}")

    user, password, databases = load_config()
    ensure_container_running()

    sql = SQL_PATH.read_text(encoding="utf-8")
    for database in databases:
        configure_reader(database, user, password, sql)
        print(f"Read-only role '{user}' configured on '{database}'.")

    print(f"Done: '{user}' is ready on {len(databases)} databases.")


if __name__ == "__main__":
    main()
