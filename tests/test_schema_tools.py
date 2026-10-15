"""queryguard.schema: the schema-discovery tools (DB-backed).

Happy-path coverage on california_schools, plus fixture-driven checks (PK/FK
separation, comments, a non-public schema, a column needing quotes, a PII column,
and the pg_stats fallback) built with the postgres admin connection in a
throwaway ``qg_test`` schema that is dropped afterwards. Every test is ``slow``
(needs the database).
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from conftest import BIRD_DBS, connect_as_admin, setting

from queryguard import schema

pytestmark = pytest.mark.slow

DB = "california_schools"
READER = setting("READER_USER", "qg_reader")

_SETUP_SQL = f"""
DROP SCHEMA IF EXISTS qg_test CASCADE;
CREATE SCHEMA qg_test;
CREATE TABLE qg_test.parent (id int PRIMARY KEY, label text);
CREATE TABLE qg_test.child (
    id int PRIMARY KEY,
    parent_id int REFERENCES qg_test.parent(id),
    email text,
    "Odd Name" text,
    note text
);
COMMENT ON TABLE qg_test.child IS 'child rows for schema-tool tests';
COMMENT ON COLUMN qg_test.child.note IS 'a free text note';
INSERT INTO qg_test.parent VALUES (1, 'a'), (2, 'b');
INSERT INTO qg_test.child VALUES
    (1, 1, 'alice@example.com', 'q1', 'hello'),
    (2, 2, 'bob@example.com', 'q2', 'world');
CREATE TABLE qg_test.nostats (id int, v text);
INSERT INTO qg_test.nostats VALUES (1, 'apple'), (2, 'banana'), (3, 'cherry');
-- A non-PII column name whose values look like emails: used to prove find_columns
-- applies value-based masking (not just column-name masking). Duplicates force the
-- value into pg_stats.most_common_vals so find_columns has a sample to mask.
CREATE TABLE qg_test.contacts (id int, contactnote text);
INSERT INTO qg_test.contacts
SELECT g, CASE WHEN g <= 10 THEN 'dup@example.com' ELSE 'plainword' END
FROM generate_series(1, 15) AS g;
ANALYZE qg_test.parent;
ANALYZE qg_test.child;
ANALYZE qg_test.contacts;
GRANT USAGE ON SCHEMA qg_test TO {READER};
GRANT SELECT ON ALL TABLES IN SCHEMA qg_test TO {READER};
"""


@pytest.fixture(scope="session", autouse=True)
def _drop_qg_test_at_session_end():
    """Safety net: whatever happens in individual tests, no qg_test schema may
    survive the session. This fixture only activates when the (slow) schema tests
    actually run — so a ``-m "not slow"`` run, which never creates qg_test and may
    not even have the database up, does not depend on it. Drops qg_test from every
    BIRD database at session end and asserts none remain.
    """
    yield
    from scripts._dockerutil import psql_capture

    leftover = []
    for database in BIRD_DBS:
        psql_capture("DROP SCHEMA IF EXISTS qg_test CASCADE", db=database)
        out = psql_capture(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'qg_test'",
            db=database,
        )
        if out.strip() == "1":
            leftover.append(database)
    assert not leftover, f"qg_test schema left behind in: {leftover}"


@pytest.fixture(autouse=True)
def _clear_cache():
    schema.clear_cache()
    yield
    schema.clear_cache()


@pytest_asyncio.fixture
async def fixtures():
    conn = await connect_as_admin(DB)
    try:
        await conn.execute(_SETUP_SQL)
    finally:
        await conn.close()
    schema.clear_cache()  # make the new schema visible to the catalog
    try:
        yield
    finally:
        schema.clear_cache()
        conn = await connect_as_admin(DB)
        try:
            await conn.execute("DROP SCHEMA IF EXISTS qg_test CASCADE;")
        finally:
            await conn.close()


def _column(description: dict, name: str) -> dict:
    return next(c for c in description["columns"] if c["name"] == name)


# --------------------------------------------------------------------------- #
# Happy path for every tool
# --------------------------------------------------------------------------- #
async def test_list_tables_happy_path() -> None:
    tables = await schema.list_tables(DB)
    names = {t["table"] for t in tables}
    assert {"schools", "frpm", "satscores"} <= names
    schools = next(t for t in tables if t["table"] == "schools")
    assert schools["estimated_rows"] > 0
    assert schools["columns"] > 0


async def test_describe_table_happy_path() -> None:
    desc = await schema.describe_table(DB, "schools")
    assert desc["table"] == "schools"
    assert desc["column_count"] > 0
    soc = _column(desc, "soc")
    assert soc["type"] == "text"
    assert "distinct_estimate" in soc


async def test_find_columns_happy_path() -> None:
    matches = await schema.find_columns(DB, "email")
    assert matches, "expected at least one email-ish column"
    assert all({"table", "column", "type"} <= set(m) for m in matches)
    assert any("email" in m["column"].lower() for m in matches)


async def test_validate_sql_happy_path() -> None:
    result = await schema.validate_sql(DB, "SELECT County, COUNT(*) FROM schools GROUP BY County")
    assert result["ok"] is True
    assert result["estimated_cost"] > 0
    assert result["within_cost_limit"] is True
    assert result["final_sql"].endswith("LIMIT 100")
    assert result["warnings"] == ["results are capped at 100 rows"]


async def test_validate_sql_rejects_unsafe() -> None:
    result = await schema.validate_sql(DB, "DROP TABLE schools")
    assert result["ok"] is False
    assert result["errors"]
    assert result["final_sql"] is None


# --------------------------------------------------------------------------- #
# Fixture-driven structural checks
# --------------------------------------------------------------------------- #
async def test_pk_and_fk_are_separated(fixtures) -> None:
    desc = await schema.describe_table(DB, "qg_test.child")
    id_col = _column(desc, "id")
    parent_col = _column(desc, "parent_id")
    assert id_col.get("primary_key") is True
    assert "foreign_key" not in id_col
    assert parent_col.get("foreign_key") == "parent.id"
    assert "primary_key" not in parent_col


async def test_column_comment_shown(fixtures) -> None:
    desc = await schema.describe_table(DB, "qg_test.child")
    assert desc.get("comment") == "child rows for schema-tool tests"
    assert _column(desc, "note")["comment"] == "a free text note"


async def test_non_public_schema_listed_qualified(fixtures) -> None:
    names = {t["table"] for t in await schema.list_tables(DB)}
    assert "qg_test.child" in names
    assert "qg_test.parent" in names


async def test_quoted_form_for_odd_name(fixtures) -> None:
    desc = await schema.describe_table(DB, "qg_test.child")
    odd = _column(desc, "Odd Name")
    assert odd["quoted"] == '"Odd Name"'


async def test_pii_values_never_returned(fixtures) -> None:
    desc = await schema.describe_table(DB, "qg_test.child")
    email = _column(desc, "email")
    assert email.get("pii_masked") is True
    assert "sample_values" not in email
    # The actual values must not appear anywhere in the serialized output.
    assert "alice@example.com" not in json.dumps(desc)


async def test_find_columns_masks_email_sample_values(fixtures) -> None:
    # contactnote is not a PII-looking name, but its values look like emails, so
    # find_columns must mask them (value-based rule), exactly like the executor and
    # describe_table — and never leak the raw address.
    matches = await schema.find_columns(DB, "contactnote")
    contacts = [m for m in matches if m["column"] == "contactnote"]
    assert contacts, "expected qg_test.contacts.contactnote to be found"
    samples = contacts[0].get("sample_values", [])
    assert schema.MASK in samples  # the email-looking value was masked
    assert "plainword" in samples  # non-email values are still shown
    assert "dup@example.com" not in json.dumps(matches)  # the address never leaks


async def test_pg_stats_fallback_distinct(fixtures) -> None:
    # qg_test.nostats was never ANALYZEd, so pg_stats has no row for it; the sample
    # values must come from the SELECT DISTINCT fallback.
    desc = await schema.describe_table(DB, "qg_test.nostats")
    values = set(_column(desc, "v")["sample_values"])
    assert {"apple", "banana", "cherry"} <= values


# --------------------------------------------------------------------------- #
# The widest BIRD table and the real FRPM quoted column
# --------------------------------------------------------------------------- #
async def test_frpm_quoted_form_shown() -> None:
    desc = await schema.describe_table(DB, "frpm")
    col = _column(desc, "FRPM Count (K-12)")
    assert col["quoted"] == '"FRPM Count (K-12)"'


async def test_widest_table_within_token_budget() -> None:
    desc = await schema.describe_table("european_football_2", "match")
    approx_tokens = len(json.dumps(desc)) / 4
    assert desc["column_count"] >= 100
    assert approx_tokens < 1500, f"describe_table(match) ~{approx_tokens:.0f} tokens"


async def test_wide_table_columns_filter_restores_detail() -> None:
    desc = await schema.describe_table("european_football_2", "match", columns=["home_team_goal"])
    col = _column(desc, "home_team_goal")
    assert "nullable" in col  # full detail is back for the requested column
    assert "distinct_estimate" in col


# --------------------------------------------------------------------------- #
# Error handling and injection safety
# --------------------------------------------------------------------------- #
async def test_unknown_table_suggests() -> None:
    with pytest.raises(schema.SchemaLookupError) as exc:
        await schema.describe_table(DB, "skools")
    assert "schools" in str(exc.value)


async def test_malicious_table_name_rejected_no_sql_built(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    original = schema._run

    async def spy_run(database: str, query: str, *args):
        seen.append(query)
        return await original(database, query, *args)

    monkeypatch.setattr(schema, "_run", spy_run)

    evil = 'schools"; DROP TABLE schools; --'
    with pytest.raises(schema.SchemaLookupError):
        await schema.describe_table(DB, evil)
    # Only the fixed catalog query ran; the caller's string never reached any SQL.
    assert all("DROP TABLE" not in q for q in seen)
    assert all(evil not in q for q in seen)


# --------------------------------------------------------------------------- #
# Caching and non-execution
# --------------------------------------------------------------------------- #
async def test_describe_table_cache_hit_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    original = schema._run

    async def counting_run(database: str, query: str, *args):
        calls["n"] += 1
        return await original(database, query, *args)

    monkeypatch.setattr(schema, "_run", counting_run)

    await schema.describe_table(DB, "schools")
    after_first = calls["n"]
    assert after_first > 0
    await schema.describe_table(DB, "schools")
    assert calls["n"] == after_first  # the second call was served from cache


async def test_validate_sql_never_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    from queryguard import executor

    async def boom(*_args, **_kwargs):
        raise AssertionError("validate_sql must never execute the query")

    monkeypatch.setattr(executor, "_fetch_rows", boom)
    result = await schema.validate_sql(DB, "SELECT COUNT(*) FROM schools")
    assert result["ok"] is True
    assert isinstance(result["estimated_cost"], float)
    assert result["within_cost_limit"] is True
