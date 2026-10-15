"""Owner-added safety tests that need Python, not a YAML case.

Deeply nested parentheses can exhaust Python's recursion limit while sqlglot
parses, raising RecursionError rather than a SqlglotError. check_sql must treat
that as an R2 parse failure and block the query, never let it crash out.
"""

from __future__ import annotations

from queryguard.safety import check_sql


def test_deep_nesting_is_blocked_not_crashed() -> None:
    sql = "SELECT " + "(" * 3000 + "1" + ")" * 3000
    result = check_sql(sql)
    assert result.ok is False
    assert result.errors[0].startswith("R2:")
