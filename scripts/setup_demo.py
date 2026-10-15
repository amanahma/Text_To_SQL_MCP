"""Set up the demo database: synthetic schema + seed, then the read-only role.

This WRAPS the existing security setup (``scripts/create_reader.sql``) without
changing it — the same role creation, grants, REVOKEs and read-only/timeout role
settings the BIRD databases use. It is the packaging equivalent of
``scripts/create_reader.py`` but targets the single synthetic demo database.

Two runners, chosen by environment:

* compose (default) — runs psql inside the demo ``db`` container:
      docker compose -p <project> -f <file> exec -T db psql ...
  Used by the local demo (``docker-compose.demo.yml``).
* direct (set ``QG_SETUP_DIRECT=1``) — runs a local ``psql`` client against the
  host/port in the environment. Used in CI, where PostgreSQL is a service
  container and ``psql`` is installed on the runner.

Steps (idempotent): apply ``db/demo/01_schema.sql`` and ``02_seed.sql``, then run
``scripts/create_reader.sql`` with ``-v reader_user`` / ``-v reader_password`` so the
reader role exists with exactly the same hardening as production.

Never prints the reader password. Reads every value from the environment.

Usage:
    python -m scripts.setup_demo          # local, via docker compose
    QG_SETUP_DIRECT=1 python -m scripts.setup_demo   # CI, via local psql
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = PROJECT_ROOT / "db" / "demo"
CREATE_READER_SQL = PROJECT_ROOT / "scripts" / "create_reader.sql"

DEMO_DB = os.environ.get("DEMO_DB", "queryguard_demo")
READER_USER = os.environ.get("READER_USER", "qg_reader")
COMPOSE_PROJECT = os.environ.get("QG_COMPOSE_PROJECT", "queryguard-demo")
COMPOSE_FILE = os.environ.get("QG_COMPOSE_FILE", "docker-compose.demo.yml")
COMPOSE_SERVICE = os.environ.get("QG_COMPOSE_SERVICE", "db")


def _reader_password() -> str:
    password = os.environ.get("DEMO_READER_PASSWORD") or os.environ.get("READER_PASSWORD")
    if not password:
        sys.exit(
            "Set DEMO_READER_PASSWORD (or READER_PASSWORD) in the environment before "
            "running the demo setup."
        )
    return password


def _find_docker() -> str:
    exe = shutil.which("docker")
    if exe:
        return exe
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "")
    for candidate in (
        Path(local_appdata) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe",
        Path(program_files) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    sys.exit("Could not find the 'docker' executable. Is Docker Desktop installed and on PATH?")


def _psql_argv(extra_vars: list[str]) -> list[str]:
    """Build the psql argv for the selected runner, without the SQL (piped via stdin)."""
    base = [
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        DEMO_DB,
        *extra_vars,
    ]
    if os.environ.get("QG_SETUP_DIRECT") == "1":
        host = os.environ.get("DB_HOST", "127.0.0.1")
        port = os.environ.get("DB_PORT", "5432")
        return [
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            host,
            "-p",
            port,
            "-U",
            "postgres",
            "-d",
            DEMO_DB,
            *extra_vars,
        ]
    return [
        _find_docker(),
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "-f",
        COMPOSE_FILE,
        "exec",
        "-T",
        COMPOSE_SERVICE,
        *base,
    ]


def _run_sql(label: str, sql: str, extra_vars: list[str], password: str, scrub: str = "") -> None:
    argv = _psql_argv(extra_vars)
    env = dict(os.environ)
    if os.environ.get("QG_SETUP_DIRECT") == "1":
        # libpq reads the superuser password from PGPASSWORD for the direct client.
        env.setdefault("PGPASSWORD", os.environ.get("POSTGRES_PASSWORD", ""))
    result = subprocess.run(
        argv, cwd=PROJECT_ROOT, input=sql, capture_output=True, text=True, env=env, check=False
    )
    if result.returncode != 0:
        out = result.stdout
        err = result.stderr
        if scrub:
            out = out.replace(scrub, "***")
            err = err.replace(scrub, "***")
        sys.stderr.write(out)
        sys.stderr.write(err)
        sys.exit(f"{label} failed (exit {result.returncode}).")
    print(f"  {label}: ok")


def main() -> None:
    if not CREATE_READER_SQL.is_file():
        sys.exit(f"Missing {CREATE_READER_SQL}")
    for name in ("01_schema.sql", "02_seed.sql"):
        if not (DEMO_DIR / name).is_file():
            sys.exit(f"Missing demo SQL: {DEMO_DIR / name}")

    password = _reader_password()
    runner = "direct psql" if os.environ.get("QG_SETUP_DIRECT") == "1" else "docker compose"
    print(f"Setting up demo database '{DEMO_DB}' via {runner} (reader role '{READER_USER}').")

    _run_sql("schema", (DEMO_DIR / "01_schema.sql").read_text(encoding="utf-8"), [], password)
    _run_sql("seed", (DEMO_DIR / "02_seed.sql").read_text(encoding="utf-8"), [], password)
    _run_sql(
        "reader role (create_reader.sql)",
        CREATE_READER_SQL.read_text(encoding="utf-8"),
        ["-v", f"reader_user={READER_USER}", "-v", f"reader_password={password}"],
        password,
        scrub=password,
    )
    print(f"Done: demo database '{DEMO_DB}' ready with read-only role '{READER_USER}'.")


if __name__ == "__main__":
    main()
