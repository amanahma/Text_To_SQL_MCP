"""Extra safety tests for R8's comment-stripping and render-time re-check.

These cover branches the YAML corpus does not: a dangerous call that is only a
comment (must pass, with the comment gone from safe_sql), the same call as live
code (must be blocked at R6), and _verify_rendered rejecting SQL whose re-parse
no longer satisfies R3-R6.
"""

from __future__ import annotations

import pytest

from queryguard.safety import _verify_rendered, check_sql


def test_dangerous_call_inside_comment_passes_and_is_stripped() -> None:
    # The `--` turns the rest of the line into a comment, so pg_sleep(5) is not
    # live code. R8 strips the comment, so none of it survives into safe_sql.
    result = check_sql("SELECT 1 -- */ , pg_sleep(5) /*")
    assert result.ok is True
    assert result.safe_sql is not None
    for fragment in ("pg_sleep", "/*", "--"):
        assert fragment not in result.safe_sql


def test_dangerous_call_as_live_code_is_blocked() -> None:
    # Here the block comments are empty and pg_sleep(5) is a real second column,
    # so it must be rejected at R6 (before R8 ever runs).
    result = check_sql("SELECT 1 /* */ , pg_sleep(5) /* */")
    assert result.ok is False
    assert result.errors[0].startswith("R6:")


@pytest.mark.parametrize(
    "rendered",
    [
        "SELECT 1; DELETE FROM t",  # fails R3 (two statements) at re-check
        "DELETE FROM t",  # fails R4 (root is not a read query)
        "SELECT pg_sleep(1)",  # fails R6 (dangerous function)
    ],
)
def test_verify_rendered_rejects_unsafe_rendered_sql(rendered: str) -> None:
    error = _verify_rendered(rendered, "postgres")
    assert error is not None
    assert error.startswith("R8:")
