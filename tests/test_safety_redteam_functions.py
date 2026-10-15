"""R6 is now an allow-list: dangerous functions fail closed.

This file pins two things:

1. Every function the Phase 2B red-team flagged as a gap in R6's deny-list — the
   ones that actually executed (WAL write / XID assignment / session state) and the
   ones only another layer happened to stop (large objects, dblink, filesystem,
   admin) — is now rejected by ``check_sql`` at R6.
2. The former dangerous-function deny-list (kept here only as a test fixture) is
   disjoint from the allow-list, and every name on it is still blocked.
"""

from __future__ import annotations

import pytest

from queryguard.function_allowlist import ALLOWED_FUNCTIONS
from queryguard.safety import check_sql

# Red-team function gaps: (name, a representative call). All should be rejected at
# R6 now that R6 is an allow-list.
REDTEAM_FUNCTION_CALLS: list[tuple[str, str]] = [
    ("pg_logical_emit_message", "SELECT pg_logical_emit_message(true, 'a', 'b')"),
    ("txid_current", "SELECT txid_current()"),
    ("pg_current_xact_id", "SELECT pg_current_xact_id()"),
    ("setseed", "SELECT setseed(0.5)"),
    ("lo_put", "SELECT lo_put(1, 0, 'x')"),
    ("lo_get", "SELECT lo_get(1)"),
    ("dblink_send_query", "SELECT dblink_send_query('a', 'b')"),
    ("dblink_connect_u", "SELECT dblink_connect_u('a')"),
    ("dblink_get_result", "SELECT dblink_get_result('a')"),
    ("pg_ls_logdir", "SELECT pg_ls_logdir()"),
    ("pg_ls_waldir", "SELECT pg_ls_waldir()"),
    ("pg_ls_tmpdir", "SELECT pg_ls_tmpdir()"),
    ("pg_current_logfile", "SELECT pg_current_logfile()"),
    ("pg_switch_wal", "SELECT pg_switch_wal()"),
    ("pg_create_restore_point", "SELECT pg_create_restore_point('a')"),
    ("pg_stat_reset", "SELECT pg_stat_reset()"),
    ("pg_promote", "SELECT pg_promote()"),
    ("pg_replication_origin_create", "SELECT pg_replication_origin_create('a')"),
    ("current_setting", "SELECT current_setting('search_path')"),
    ("currval", "SELECT currval('s')"),
    ("lastval", "SELECT lastval()"),
]

# The former R6 deny-list, kept ONLY as a test fixture now that R6 is an allow-list.
OLD_DANGEROUS_FUNCTIONS: frozenset[str] = frozenset(
    {
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "lo_unlink",
        "lo_create",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "set_config",
        "nextval",
        "setval",
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
        "pg_try_advisory_lock",
        "pg_notify",
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
    }
)


@pytest.mark.parametrize(
    "name,sql", REDTEAM_FUNCTION_CALLS, ids=[n for n, _ in REDTEAM_FUNCTION_CALLS]
)
def test_redteam_function_blocked_by_r6(name: str, sql: str) -> None:
    result = check_sql(sql)
    assert result.ok is False, f"{name} should be blocked by R6"
    assert result.safe_sql is None
    assert result.errors[0].startswith("R6:"), result.errors
    assert name in result.errors[0], result.errors[0]


def test_allowlist_and_old_denylist_are_disjoint() -> None:
    overlap = ALLOWED_FUNCTIONS & OLD_DANGEROUS_FUNCTIONS
    assert not overlap, f"allow-list must not contain any former dangerous name: {sorted(overlap)}"


@pytest.mark.parametrize("name", sorted(OLD_DANGEROUS_FUNCTIONS))
def test_old_dangerous_function_still_blocked(name: str) -> None:
    # Not on the allow-list ...
    assert name not in ALLOWED_FUNCTIONS
    # ... and a real call to it is rejected at R6.
    result = check_sql(f"SELECT {name}(1)")
    assert result.ok is False
    assert result.errors[0].startswith("R6:"), result.errors
