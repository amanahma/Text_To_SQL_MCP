"""QueryGuard's main MCP server.

A thin MCP surface over the safety stack. Discovery tools (list_databases,
list_tables), schema tools (describe_table, find_columns) and validate_sql help a
model understand a database and check a query before running it; execute_query
runs a checked, read-only query. All logic lives in queryguard.schema and
queryguard.executor — this module only adapts it to MCP and turns the typed
failures into tool errors.

Transport is stdio (``uv run python -m queryguard.server``). The MCP protocol
owns stdout, so nothing here may ever ``print``; all diagnostics go to stderr.
"""

from __future__ import annotations

import logging
import sys

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from queryguard import schema
from queryguard.db import UnknownDatabaseError, allowed_databases
from queryguard.executor import run_checked
from queryguard.schema import SchemaLookupError

logger = logging.getLogger("queryguard")

READ_ONLY = ToolAnnotations(readOnlyHint=True)


def build_server() -> FastMCP:
    """Construct the main QueryGuard MCP server with its discovery tools."""
    mcp = FastMCP(
        name="queryguard",
        instructions=(
            "Safe read-only access to a set of PostgreSQL databases. Start with "
            "list_databases, then list_tables(database) to explore, and pass the "
            "chosen database name to every tool that takes one."
        ),
    )

    @mcp.tool(
        name="list_databases",
        description=(
            "List the names of the PostgreSQL databases you can query. "
            "Call this first; use one of the returned names as the 'database' "
            "argument to the other tools."
        ),
        annotations=READ_ONLY,
    )
    async def list_databases() -> list[str]:
        return allowed_databases()

    @mcp.tool(
        name="list_tables",
        description=(
            "List every table in a database (tables outside the 'public' schema are "
            "shown schema-qualified, e.g. 'myschema.t'). Each entry has the table "
            "name, an estimated row count, its column count, and a table comment if "
            "one exists. Pass 'database' from list_databases. Use this to see what "
            "exists, then describe_table to understand a table's columns."
        ),
        annotations=READ_ONLY,
    )
    async def list_tables(database: str) -> list[dict]:
        try:
            return await schema.list_tables(database)
        except (UnknownDatabaseError, SchemaLookupError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="describe_table",
        description=(
            "Describe one table's columns before you write SQL. For each column: "
            "name, type, nullability, primary-key flag, foreign key (referenced "
            "table.column), any column comment, null fraction, estimated distinct "
            "count, and a few sample values (never for PII columns). A column whose "
            "name has spaces or capitals is shown with the exact double-quoted form "
            "to use in SQL (PostgreSQL folds unquoted identifiers to lower case, so "
            "such names MUST be double-quoted). Pass 'table' as a name from "
            "list_tables; an unknown name returns suggestions. Very wide tables omit "
            "per-column detail — pass columns=[...] to get it for specific columns."
        ),
        annotations=READ_ONLY,
    )
    async def describe_table(database: str, table: str, columns: list[str] | None = None) -> dict:
        try:
            return await schema.describe_table(database, table, columns)
        except (UnknownDatabaseError, SchemaLookupError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="find_columns",
        description=(
            "Search across all tables for columns matching a keyword (case-insensitive "
            "substring plus fuzzy matching on column names, table names and comments). "
            "Returns up to 20 matches, each with its table, column, type and a few "
            "sample values (never for PII columns). Use this when you know the concept "
            "you need (e.g. 'email', 'enrollment', 'salary') but not the exact column "
            "name — BIRD columns are often abbreviated."
        ),
        annotations=READ_ONLY,
    )
    async def find_columns(database: str, keyword: str) -> list[dict]:
        try:
            return await schema.find_columns(database, keyword)
        except (UnknownDatabaseError, SchemaLookupError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="validate_sql",
        description=(
            "Check a query WITHOUT running it: returns whether it passes the safety "
            "rules, any errors or warnings, the planner's estimated cost against the "
            "limit, and the exact final SQL that would run (with its row cap applied). "
            "Use this to catch mistakes before execute_query, especially for a query "
            "you expect to be expensive. It never returns rows and never executes the "
            "query."
        ),
        annotations=READ_ONLY,
    )
    async def validate_sql(database: str, sql: str) -> dict:
        try:
            return await schema.validate_sql(database, sql)
        except (UnknownDatabaseError, SchemaLookupError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="execute_query",
        description=(
            "Run one read-only SQL query against a database and return the rows. "
            "Pass 'database' (a name from list_databases) and 'sql', a single "
            "PostgreSQL 16 SELECT statement (CTEs, UNION/INTERSECT/EXCEPT and "
            "set-returning functions are fine; no INSERT/UPDATE/DDL, no multiple "
            "statements). Use describe_table to get exact column names and types "
            "first, and validate_sql to check an expensive query before running it. "
            "PostgreSQL facts: an identifier with spaces or capital letters must be "
            'double-quoted (e.g. "FRPM Count (K-12)"); ORDER BY ... DESC lists NULLs '
            "first unless you add NULLS LAST; integer / integer truncates, so cast to "
            "numeric for a ratio (e.g. col::numeric / total). Results are capped at "
            "100 rows: when 'truncated' is true there were more, so add a LIMIT, "
            "filter, or aggregate. A 5-second time limit applies. On failure the tool "
            "returns an error whose message starts with a short reason "
            "(blocked/cost/timeout/sql_error) and says what to change; read it and fix "
            "the query rather than retrying the same SQL."
        ),
        annotations=READ_ONLY,
    )
    async def execute_query(database: str, sql: str) -> dict[str, object]:
        try:
            result = await run_checked(database, sql)
        except UnknownDatabaseError as exc:
            raise ToolError(str(exc)) from exc
        if not result.ok:
            # An actionable, single-line message; the error_kind prefixes it so the
            # model can tell a safety refusal from a plain SQL mistake.
            raise ToolError(f"{result.error_kind}: {result.error}")
        return {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "warnings": result.warnings,
            "elapsed_ms": result.elapsed_ms,
        }

    return mcp


mcp = build_server()


def main() -> None:
    """Run the server over stdio. Logs go to stderr only."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
