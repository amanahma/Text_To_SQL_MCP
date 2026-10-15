"""Create (or refresh) the PLAIN control role ``qg_plain`` on every BIRD database.

This is the control role for the "second attack setting" (side_effect and
system_access categories). It simulates an UNHARDENED database: CONNECT + USAGE +
SELECT, no read-only default, no statement_timeout, and EXECUTE granted back on
exactly the functions create_reader.sql revoked FROM PUBLIC (granted to qg_plain
only — never PUBLIC, never qg_reader). See scripts/create_plain.sql for the detail.

Reads PLAIN_USER / PLAIN_PASSWORD / BIRD_DBS (and READER_USER) from .env, then runs
``scripts/create_plain.sql`` with psql inside the Docker container once per
database. Idempotent. Never touches qg_reader or PUBLIC. Afterwards it prints, per
database, has_function_privilege(EXECUTE) for qg_plain and qg_reader on each of
those functions and exits non-zero unless qg_plain is t and qg_reader is f for all.

Usage:
    uv run python scripts/create_plain.py
"""

from __future__ import annotations

import subprocess
import sys

from _dockerutil import COMPOSE_SERVICE, PROJECT_ROOT, compose_base
from dotenv import dotenv_values

SQL_PATH = PROJECT_ROOT / "scripts" / "create_plain.sql"
ENV_PATH = PROJECT_ROOT / ".env"


# The functions create_reader.sql revokes EXECUTE on FROM PUBLIC (verification set).
REVOKED_FUNCTIONS = ("pg_logical_emit_message", "txid_current", "pg_current_xact_id", "setseed")


def load_config() -> tuple[str, str, list[str], str]:
    values = dotenv_values(ENV_PATH)
    user = values.get("PLAIN_USER")
    password = values.get("PLAIN_PASSWORD")
    dbs_raw = values.get("BIRD_DBS")
    reader_user = values.get("READER_USER") or "qg_reader"
    if not user or not password or not dbs_raw:
        sys.exit(
            f"Missing PLAIN_USER / PLAIN_PASSWORD / BIRD_DBS in {ENV_PATH}. "
            "Add PLAIN_USER and PLAIN_PASSWORD (see .env.example) and fill them in."
        )
    databases = [name.strip() for name in dbs_raw.split(",") if name.strip()]
    if not databases:
        sys.exit(f"BIRD_DBS is empty in {ENV_PATH}.")
    return user, password, databases, reader_user


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


def configure_plain(database: str, user: str, password: str, sql: str) -> None:
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
        f"plain_user={user}",
        "-v",
        f"plain_password={password}",
    ]
    result = subprocess.run(
        cmd, cwd=PROJECT_ROOT, input=sql, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        # Scrub the password from anything we print, defence-in-depth.
        sys.stderr.write(result.stdout.replace(password, "***"))
        sys.stderr.write(result.stderr.replace(password, "***"))
        sys.exit(f"create_plain.sql failed on '{database}' (exit {result.returncode}).")


def _verify_sql(plain_user: str, reader_user: str) -> str:
    # Role names come from .env (trusted config); inline them as SQL string literals.
    # (psql does not interpolate :'var' inside a -c command string across versions.)
    names = ", ".join(f"'{fn}'" for fn in REVOKED_FUNCTIONS)
    return (
        "SELECT p.oid::regprocedure::text || '|' "
        f"|| has_function_privilege('{plain_user}', p.oid, 'EXECUTE')::text || '|' "
        f"|| has_function_privilege('{reader_user}', p.oid, 'EXECUTE')::text "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        f"WHERE n.nspname = 'pg_catalog' AND p.proname IN ({names}) ORDER BY 1"
    )


def verify_privileges(database: str, plain_user: str, reader_user: str) -> bool:
    """Print has_function_privilege(EXECUTE) for plain/reader; True iff plain=t, reader=f."""
    cmd = compose_base() + [
        "exec",
        "-T",
        COMPOSE_SERVICE,
        "psql",
        "-U",
        "postgres",
        "-d",
        database,
        "-tAc",
        _verify_sql(plain_user, reader_user),
    ]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(f"privilege verification failed on '{database}' (exit {result.returncode}).")
    ok = True
    print(f"  [{database}]")
    for line in result.stdout.strip().splitlines():
        # has_function_privilege(...)::text renders 'true'/'false'.
        sig, plain_priv, reader_priv = line.split("|")
        as_expected = plain_priv == "true" and reader_priv == "false"
        flag = "" if as_expected else "  <-- UNEXPECTED"
        ok = ok and as_expected
        print(f"    {sig:<45} {plain_user}={plain_priv} {reader_user}={reader_priv}{flag}")
    return ok


def main() -> None:
    if not SQL_PATH.is_file():
        sys.exit(f"SQL script not found: {SQL_PATH}")

    user, password, databases, reader_user = load_config()
    ensure_container_running()

    sql = SQL_PATH.read_text(encoding="utf-8")
    for database in databases:
        configure_plain(database, user, password, sql)
        print(f"Plain control role '{user}' configured on '{database}'.")

    print(f"Done: '{user}' is ready on {len(databases)} databases.\n")
    print(f"EXECUTE on the REVOKE'd functions ({user} must be t, {reader_user} must be f):")
    checks = [verify_privileges(db, user, reader_user) for db in databases]
    if not all(checks):
        sys.exit("Privilege check FAILED: expected qg_plain=t and qg_reader=f everywhere.")
    print(
        f"\nOK: {user}=t and {reader_user}=f for every function in all {len(databases)} databases."
    )


if __name__ == "__main__":
    main()
