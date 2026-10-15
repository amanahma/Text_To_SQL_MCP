"""Verify the demo database's read-only role is correctly locked down.

Connects as the reader role (qg_reader) to the demo database and asserts the
hardening properties, printing one line per check and exiting non-zero if any
check fails. Connection settings come from the environment (DB_HOST, DB_PORT,
READER_USER, READER_PASSWORD, DEMO_DB / BIRD_DBS).

Checks:
  1. the role is NOT a superuser
  2. INSERT / UPDATE / DELETE / CREATE are denied at execution time, and the
     denial is a privilege or read-only-transaction error (any other error fails)
  3. privilege checks that do NOT depend on the read-only transaction:
       - has_table_privilege INSERT/UPDATE/DELETE/TRUNCATE is false for every
         public table
       - has_schema_privilege(public, CREATE) is false
       - has_database_privilege(CREATE) is false
       - database TEMP privilege is printed as INFO (not asserted)
  4. default_transaction_read_only is 'on' (if create_reader.sql sets it)
  5. EXECUTE is denied on every function the existing setup revokes
     (the list is read from scripts/create_reader.sql and printed)
  6. statement_timeout is non-zero and at most 30 s

Run inside the server image:
    python -m scripts.verify_setup
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import asyncpg

CREATE_READER_SQL = Path(__file__).resolve().parent / "create_reader.sql"

# Errors that constitute a legitimate write denial. Anything else (e.g. a
# NotNullViolationError) means the write was attempted for real and is a FAIL.
_DENIAL_ERRORS = (asyncpg.InsufficientPrivilegeError, asyncpg.ReadOnlySQLTransactionError)

# statement_timeout unit -> milliseconds (a unitless value is milliseconds in PG).
_UNIT_MS = {"": 1, "ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}
_MAX_TIMEOUT_MS = 30_000  # 30 s


def _target_database() -> str:
    if os.environ.get("DEMO_DB"):
        return os.environ["DEMO_DB"]
    bird = os.environ.get("BIRD_DBS", "").split(",")
    first = bird[0].strip() if bird and bird[0].strip() else ""
    return first or "queryguard_demo"


def _connect_kwargs() -> dict[str, object]:
    password = os.environ.get("READER_PASSWORD")
    if not password:
        sys.exit("READER_PASSWORD is not set in the environment.")
    return {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "user": os.environ.get("READER_USER", "qg_reader"),
        "password": password,
        "database": _target_database(),
    }


def _revoked_function_names() -> list[str]:
    """Read the EXECUTE-revoked function names straight from create_reader.sql."""
    text = CREATE_READER_SQL.read_text(encoding="utf-8")
    match = re.search(r"p\.proname\s+IN\s*\((.*?)\)", text, re.DOTALL)
    if not match:
        sys.exit("Could not find the revoked-function list in create_reader.sql.")
    return re.findall(r"'([^']+)'", match.group(1))


def _create_reader_sets_read_only() -> bool:
    """True if create_reader.sql sets default_transaction_read_only = on for the role."""
    text = CREATE_READER_SQL.read_text(encoding="utf-8")
    return re.search(r"default_transaction_read_only\s*=\s*on", text, re.IGNORECASE) is not None


def _parse_timeout_ms(value: str | None) -> int | None:
    """Parse a statement_timeout value (e.g. '30s', '500ms', '0') to milliseconds."""
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*(ms|s|min|h|d)?\s*", value)
    if not match:
        return None
    return int(match.group(1)) * _UNIT_MS[match.group(2) or ""]


class Checker:
    def __init__(self) -> None:
        self.failures = 0

    def record(self, name: str, passed: bool, detail: str) -> None:
        marker = "PASS" if passed else "FAIL"
        if not passed:
            self.failures += 1
        print(f"[{marker}] {name}: {detail}")

    def info(self, name: str, detail: str) -> None:
        print(f"[INFO] {name}: {detail}")

    async def write_is_denied(self, conn: asyncpg.Connection, sql: str) -> tuple[bool, str]:
        """PASS only if the write raised a privilege or read-only-transaction error."""
        try:
            await conn.execute(sql)
        except _DENIAL_ERRORS as exc:
            return True, f"denied ({exc.__class__.__name__}: {str(exc).splitlines()[0]})"
        except asyncpg.PostgresError as exc:
            return False, (
                f"WRONG ERROR — not a privilege/read-only denial "
                f"({exc.__class__.__name__}: {str(exc).splitlines()[0]})"
            )
        return False, "SUCCEEDED — the write was NOT denied"


async def run() -> int:
    kwargs = _connect_kwargs()
    checker = Checker()
    print(
        f"Verifying reader role '{kwargs['user']}' on database "
        f"'{kwargs['database']}' at {kwargs['host']}:{kwargs['port']}.\n"
    )
    try:
        conn = await asyncpg.connect(**kwargs)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"[FAIL] connect: could not connect as the reader role: {exc}")
        return 1

    try:
        # 1. not a superuser
        is_super = await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        checker.record("not superuser", is_super is False, f"rolsuper = {is_super}")

        # 2. Execution-time write denials (must be a privilege / read-only error).
        probe = await conn.fetchrow(
            "SELECT quote_ident(table_name) AS t, quote_ident(column_name) AS c "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "ORDER BY table_name, ordinal_position LIMIT 1"
        )
        if probe is None:
            checker.record(
                "write probes", False, "no public table found to probe INSERT/UPDATE/DELETE"
            )
        else:
            table, column = probe["t"], probe["c"]
            for op, sql in (
                ("INSERT denied", f"INSERT INTO {table} DEFAULT VALUES"),
                ("UPDATE denied", f"UPDATE {table} SET {column} = {column} WHERE false"),
                ("DELETE denied", f"DELETE FROM {table} WHERE false"),
            ):
                passed, detail = await checker.write_is_denied(conn, sql)
                checker.record(op, passed, detail)
        passed, detail = await checker.write_is_denied(
            conn, "CREATE TABLE _qg_verify_probe (x integer)"
        )
        checker.record("CREATE denied", passed, detail)

        # 3. Privilege checks independent of the read-only transaction.
        priv_rows = await conn.fetch(
            "SELECT c.relname AS name, "
            "       has_table_privilege(current_user, c.oid, 'INSERT') AS ins, "
            "       has_table_privilege(current_user, c.oid, 'UPDATE') AS upd, "
            "       has_table_privilege(current_user, c.oid, 'DELETE') AS del, "
            "       has_table_privilege(current_user, c.oid, 'TRUNCATE') AS trunc "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"
        )
        if not priv_rows:
            checker.record("table privileges", False, "no public base tables found to check")
        else:
            for priv, key in (
                ("INSERT", "ins"),
                ("UPDATE", "upd"),
                ("DELETE", "del"),
                ("TRUNCATE", "trunc"),
            ):
                granted = [r["name"] for r in priv_rows if r[key]]
                checker.record(
                    f"has_table_privilege {priv} false for all public tables",
                    not granted,
                    f"{len(priv_rows)} table(s), none grant {priv}"
                    if not granted
                    else f"{priv} GRANTED on: {', '.join(granted)}",
                )

        schema_create = await conn.fetchval(
            "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
        )
        checker.record(
            "has_schema_privilege(public, CREATE) is false",
            schema_create is False,
            f"CREATE on schema public = {schema_create}",
        )
        db_create = await conn.fetchval(
            "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
        )
        checker.record(
            "has_database_privilege(CREATE) is false",
            db_create is False,
            f"CREATE on database = {db_create}",
        )
        db_temp = await conn.fetchval(
            "SELECT has_database_privilege(current_user, current_database(), 'TEMP')"
        )
        checker.info("database TEMP privilege (not asserted)", f"TEMP on database = {db_temp}")

        # 4. default_transaction_read_only.
        dtro = await conn.fetchval("SHOW default_transaction_read_only")
        if _create_reader_sets_read_only():
            checker.record(
                "default_transaction_read_only is 'on'",
                dtro == "on",
                f"default_transaction_read_only = {dtro!r}. NOTE: a direct client can turn "
                "this off; the privilege checks above are the real proof.",
            )
        else:
            checker.info(
                "default_transaction_read_only",
                f"= {dtro!r} (create_reader.sql does not set it; not asserted). A direct "
                "client can turn this off; the privilege checks above are the real proof.",
            )

        # 5. EXECUTE denied on each revoked function (every overload).
        names = _revoked_function_names()
        print(f"\n  Revoked-function list (from create_reader.sql): {', '.join(names)}\n")
        rows = await conn.fetch(
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args, "
            "       has_function_privilege(current_user, p.oid, 'EXECUTE') AS can_exec "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'pg_catalog' AND p.proname = ANY($1::text[]) "
            "ORDER BY p.proname, args",
            names,
        )
        for name in names:
            overloads = [r for r in rows if r["proname"] == name]
            if not overloads:
                checker.record(f"EXECUTE denied: {name}", False, "function not found in pg_catalog")
                continue
            granted = [r for r in overloads if r["can_exec"]]
            detail = "; ".join(f"{name}({r['args']})" for r in overloads)
            checker.record(
                f"EXECUTE denied: {name}",
                not granted,
                f"all {len(overloads)} overload(s) denied [{detail}]"
                if not granted
                else f"EXECUTE still GRANTED on: {', '.join(name + '(' + r['args'] + ')' for r in granted)}",
            )

        # 6. statement_timeout: non-zero and at most 30 s.
        timeout = await conn.fetchval("SHOW statement_timeout")
        ms = _parse_timeout_ms(timeout)
        bounded = ms is not None and 0 < ms <= _MAX_TIMEOUT_MS
        checker.record(
            "statement_timeout non-zero and <= 30 s",
            bounded,
            f"statement_timeout = {timeout!r} ({ms} ms). NOTE: this is the role default; "
            "the executor narrows it to '5s' per query via SET LOCAL (executor.py), not "
            "observable on a bare role connection.",
        )
    finally:
        await conn.close()

    print()
    if checker.failures:
        print(f"RESULT: {checker.failures} check(s) FAILED.")
        return 1
    print("RESULT: all checks passed.")
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
