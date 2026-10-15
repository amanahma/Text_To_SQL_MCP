"""The database must refuse the Phase 2B side-effect functions for qg_reader.

check_sql's R6 allow-list already blocks these, but defence in depth means the
database should refuse them too: scripts/create_reader.sql revokes EXECUTE on every
signature of pg_logical_emit_message, txid_current, pg_current_xact_id and setseed.
Run create_reader.py if these fail (the revoke may not have been applied yet).
"""

from __future__ import annotations

import asyncpg
import pytest
from conftest import BIRD_DBS

# Check against (at least) two databases, to prove the revoke was applied per db.
# Every test here needs the live database, so the whole module is marked slow.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.parametrize("reader_conn", BIRD_DBS[:2], indirect=True),
]

# A call to each revoked function. Each must raise a permission error for qg_reader.
REVOKED_CALLS: list[tuple[str, str]] = [
    ("pg_logical_emit_message", "SELECT pg_logical_emit_message(true, 'qg', 'test')"),
    ("txid_current", "SELECT txid_current()"),
    ("pg_current_xact_id", "SELECT pg_current_xact_id()"),
    ("setseed", "SELECT setseed(0.5)"),
]


@pytest.mark.parametrize("name,sql", REVOKED_CALLS, ids=[n for n, _ in REVOKED_CALLS])
async def test_revoked_function_denied(
    reader_conn: asyncpg.Connection, name: str, sql: str
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await reader_conn.fetchval(sql)
