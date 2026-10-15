"""End-to-end MCP smoke test over the SAME stdio transport the Docker image uses.

Spawns the server exactly as the image's entrypoint does — ``python -m
queryguard.server`` over stdio — and drives it with a FastMCP client against the
synthetic demo database. Prints one line per check and exits non-zero on any
failure.

Checks:
  1. exactly the 6 expected tools are listed
  2. list_tables works on the demo database
  3. a simple SELECT returns rows
  4. DROP TABLE is blocked by check_sql rule R4 (rule id checked)
  5. SELECT pg_ls_dir('.') is blocked by check_sql rule R6 (rule id checked)
  6. a generate_series(1, 1e9) query is reported over the cost limit
  7. a plain SELECT of the fake-email column is masked
     (basic masking path only; known bypasses (alias, substr, to_jsonb) are NOT
      tested here)

Run inside the server image:
    python -m scripts.smoke_mcp
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

# check_sql rejections start "blocked: R<n>: ...". This pulls the R1–R8 rule id.
_RULE_RE = re.compile(r"\b(R[1-8])\b")


def _rule_id(text: str) -> str | None:
    match = _RULE_RE.search(text)
    return match.group(1) if match else None


EXPECTED_TOOLS = {
    "list_databases",
    "list_tables",
    "describe_table",
    "find_columns",
    "validate_sql",
    "execute_query",
}


def _database() -> str:
    if os.environ.get("DEMO_DB"):
        return os.environ["DEMO_DB"]
    bird = os.environ.get("BIRD_DBS", "").split(",")
    first = bird[0].strip() if bird and bird[0].strip() else ""
    return first or "queryguard_demo"


class Smoke:
    def __init__(self) -> None:
        self.failures = 0

    def record(self, name: str, passed: bool, detail: str) -> None:
        marker = "PASS" if passed else "FAIL"
        if not passed:
            self.failures += 1
        print(f"[{marker}] {name}: {detail}")


def _result_data(res: object) -> object:
    """Best-effort structured payload from a FastMCP CallToolResult."""
    data = getattr(res, "data", None)
    if data is not None:
        return data
    text = "".join(getattr(c, "text", "") or "" for c in getattr(res, "content", []) or [])
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


async def run() -> int:
    database = _database()
    smoke = Smoke()
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "queryguard.server"],
        env=dict(os.environ),  # pass BIRD_DBS / DB_* / READER_* through to the server
    )
    print(f"Smoke-testing the MCP server (stdio) against database '{database}'.\n")

    async with Client(transport) as client:
        # 1. exactly the 6 expected tools
        tools = await client.list_tools()
        names = {t.name for t in tools}
        smoke.record(
            "exactly 6 expected tools",
            names == EXPECTED_TOOLS,
            f"{sorted(names)}"
            if names == EXPECTED_TOOLS
            else f"got {sorted(names)}; expected {sorted(EXPECTED_TOOLS)}",
        )

        # 2. list_tables works
        res = await client.call_tool("list_tables", {"database": database}, raise_on_error=False)
        data = _result_data(res)
        ok = (not res.is_error) and isinstance(data, list) and len(data) > 0
        smoke.record(
            "list_tables works",
            ok,
            f"{len(data)} tables" if ok else f"error/empty: {data}",
        )

        # 3. a simple SELECT returns rows
        res = await client.call_tool(
            "execute_query",
            {
                "database": database,
                "sql": "SELECT book_id, title FROM books ORDER BY book_id LIMIT 5",
            },
            raise_on_error=False,
        )
        data = _result_data(res)
        ok = (not res.is_error) and isinstance(data, dict) and data.get("row_count", 0) > 0
        smoke.record(
            "simple SELECT returns rows",
            ok,
            f"{data.get('row_count')} rows" if ok else f"error: {data}",
        )

        # 4 & 5. Each must be blocked by check_sql with the EXPECTED rule id (R4 for
        # DROP, R6 for pg_ls_dir), read from the error text. No rule id -> stop.
        for name, sql, expected in (
            ("DROP TABLE blocked by rule R4", "DROP TABLE books", "R4"),
            ("pg_ls_dir('.') blocked by rule R6", "SELECT pg_ls_dir('.')", "R6"),
        ):
            res = await client.call_tool(
                "execute_query", {"database": database, "sql": sql}, raise_on_error=False
            )
            text = str(_result_data(res))
            if not res.is_error:
                smoke.record(name, False, f"NOT blocked — returned ok: {text}")
                continue
            rule = _rule_id(text)
            if rule is None:
                print(
                    f"[STOP] {name}: the block carried NO rule id. Error text: {text!r}. "
                    "Cannot confirm this is a check_sql rejection — stopping."
                )
                return 1
            smoke.record(
                name,
                rule == expected,
                f"rule id = {rule}; error: {text}"
                if rule == expected
                else f"WRONG rule id {rule} (expected {expected}); error: {text}",
            )

        # 6. generate_series(1, 1e9) reported over the cost limit (via validate_sql)
        res = await client.call_tool(
            "validate_sql",
            {"database": database, "sql": "SELECT count(*) FROM generate_series(1, 1000000000)"},
            raise_on_error=False,
        )
        data = _result_data(res)
        within = isinstance(data, dict) and data.get("within_cost_limit") is False
        smoke.record(
            "generate_series(1,1e9) over cost limit",
            within,
            f"within_cost_limit={data.get('within_cost_limit')}, "
            f"estimated_cost={data.get('estimated_cost')}, cost_limit={data.get('cost_limit')}"
            if isinstance(data, dict)
            else f"unexpected result: {data}",
        )

        # 7. plain SELECT of the fake-email column is masked
        #    (basic masking path only; known bypasses (alias, substr, to_jsonb) are NOT tested here)
        res = await client.call_tool(
            "execute_query",
            {
                "database": database,
                "sql": "SELECT email FROM customers ORDER BY customer_id LIMIT 5",
            },
            raise_on_error=False,
        )
        data = _result_data(res)
        masked = False
        detail = f"error: {data}"
        if (not res.is_error) and isinstance(data, dict) and "email" in data.get("columns", []):
            idx = data["columns"].index("email")
            values = [row[idx] for row in data.get("rows", [])]
            masked = len(values) > 0 and all(v == "***" for v in values)
            detail = f"email cells = {values}"
        smoke.record(
            "fake-email column is masked (basic path only; "
            "alias/substr/to_jsonb bypasses NOT tested here)",
            masked,
            detail,
        )

    print()
    if smoke.failures:
        print(f"RESULT: {smoke.failures} check(s) FAILED.")
        return 1
    print("RESULT: all checks passed.")
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
