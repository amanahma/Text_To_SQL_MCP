"""Drop the PLAIN control role ``qg_plain`` cleanly.

A role cannot be dropped while it still holds privileges in any database, so we
run ``DROP OWNED BY`` in every BIRD database first (scripts/drop_plain.sql), then
``DROP ROLE`` once at the end. Reads PLAIN_USER / BIRD_DBS from .env. Idempotent:
safe to run when qg_plain does not exist. Never touches qg_reader.

Usage:
    uv run python -m scripts.drop_plain      (or: uv run python scripts/drop_plain.py)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Make the sibling _dockerutil importable whether run as a path (scripts/ is sys.path[0])
# or as a module `-m scripts.drop_plain` (project root is sys.path[0], scripts/ is not).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _dockerutil import COMPOSE_SERVICE, PROJECT_ROOT, compose_base
from dotenv import dotenv_values

SQL_PATH = PROJECT_ROOT / "scripts" / "drop_plain.sql"
ENV_PATH = PROJECT_ROOT / ".env"


def load_config() -> tuple[str, list[str]]:
    values = dotenv_values(ENV_PATH)
    user = values.get("PLAIN_USER")
    dbs_raw = values.get("BIRD_DBS")
    if not user or not dbs_raw:
        sys.exit(f"Missing PLAIN_USER / BIRD_DBS in {ENV_PATH}.")
    databases = [name.strip() for name in dbs_raw.split(",") if name.strip()]
    if not databases:
        sys.exit(f"BIRD_DBS is empty in {ENV_PATH}.")
    return user, databases


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


def _psql(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    cmd = compose_base() + [
        "exec",
        "-T",
        COMPOSE_SERVICE,
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        *args,
    ]
    return subprocess.run(
        cmd, cwd=PROJECT_ROOT, input=stdin, capture_output=True, text=True, check=False
    )


def drop_owned(database: str, user: str, sql: str) -> None:
    result = _psql(["-d", database, "-v", f"plain_user={user}"], stdin=sql)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(f"drop_plain.sql failed on '{database}' (exit {result.returncode}).")


def drop_role(user: str) -> None:
    # Cluster-global; guarded so it is a no-op when the role is already gone.
    stmt = (
        "SELECT 'DROP ROLE ' || quote_ident(:'plain_user') "
        "WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'plain_user')\\gexec"
    )
    result = _psql(["-d", "postgres", "-v", f"plain_user={user}"], stdin=stmt)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(f"DROP ROLE failed (exit {result.returncode}).")


def main() -> None:
    if not SQL_PATH.is_file():
        sys.exit(f"SQL script not found: {SQL_PATH}")

    user, databases = load_config()
    ensure_container_running()

    sql = SQL_PATH.read_text(encoding="utf-8")
    for database in databases:
        drop_owned(database, user, sql)
        print(f"Revoked '{user}' privileges on '{database}'.")
    drop_role(user)
    print(f"Done: '{user}' dropped (if it existed).")


if __name__ == "__main__":
    main()
