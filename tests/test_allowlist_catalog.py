"""Verify ALLOWED_FUNCTIONS against PostgreSQL's own catalog (pg_proc).

Runs as ``qg_reader`` against california_schools; ``pg_proc`` is cluster-wide and
readable by every role, so this needs the database. It checks two invariants that a
hand-maintained allow-list can silently break:

1. **Volatility.** A ``provolatile = 'v'`` function may have side effects. Every
   allow-listed name that has ANY volatile signature must be listed in
   ``REVIEWED_VOLATILE`` with a one-line reason — written only after checking the
   PostgreSQL 16 docs — confirming it is side-effect-free. A newly volatile or
   unreviewed name fails the test (and must NOT be waved through: if it could have a
   side effect it does not belong in the allow-list at all).

2. **Names absent from pg_proc.** These are fine only if they are sqlglot's own
   canonical function names or SQL operators/keywords (``SQLGLOT_INTERNAL_NAMES``,
   with a reason each). Anything else not found is a likely typo and fails the test.

Both sets were derived from the live PG16 catalog and the PG16 documentation; the
tests assert the catalog still matches them exactly, so drift (a renamed, retyped,
or mistyped entry) shows up as a failure naming the offender.
"""

from __future__ import annotations

import asyncpg
import pytest

from queryguard.function_allowlist import ALLOWED_FUNCTIONS

# Check against california_schools specifically (pg_proc is identical on every db).
# Every test here reads pg_proc over a live connection, so the module is marked slow.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.parametrize("reader_conn", ["california_schools"], indirect=True),
]

# Allow-listed names with a volatile (provolatile='v') signature in PG16. Each is
# volatile only because its result varies per call — none has a side effect (checked
# against the PG16 "Date/Time Functions" and "Mathematical Functions" docs).
REVIEWED_VOLATILE: dict[str, str] = {
    "random": "returns a random number in [0,1); result varies per call, no side effects",
    "clock_timestamp": "returns the current wall-clock time (changes mid-statement); read-only",
    "timeofday": "returns the current wall-clock time as text; read-only, no side effects",
}

# Allow-listed names that are NOT in pg_proc. Acceptable only because they are
# sqlglot's canonical function names, or SQL operators/keywords sqlglot models as
# Func nodes — never a real callable that could be dangerous.
SQLGLOT_INTERNAL_NAMES: dict[str, str] = {
    # --- SQL operators / keywords (no pg_proc entry) ----------------------- #
    "and": "boolean AND operator",
    "or": "boolean OR operator",
    "xor": "boolean XOR; sqlglot operator node",
    "case": "CASE expression; sqlglot models it as a Func",
    "if": "CASE/IF expression; sqlglot node",
    "exists": "EXISTS(subquery) predicate",
    "any": "= ANY(...) quantified comparison operator",
    "all": ">= ALL(...) quantified comparison operator",
    "some": "= SOME(...) quantified comparison operator",
    "collate": "COLLATE clause; SQL keyword",
    "cast": "CAST(x AS t) expression; SQL keyword, not a catalog function",
    "try_cast": "sqlglot TRY_CAST node; rendered as a cast, not a catalog function",
    "coalesce": "COALESCE expression; SQL construct, not a pg_proc function",
    "nullif": "NULLIF expression; SQL construct",
    "greatest": "GREATEST expression; SQL construct",
    "least": "LEAST expression; SQL construct",
    "grouping": "GROUPING(...) grouping-sets construct; not a pg_proc function",
    "current_date": "SQL datetime keyword (no parens); not a pg_proc function",
    "current_time": "SQL datetime keyword; not a pg_proc function",
    "current_timestamp": "SQL datetime keyword; now() is the catalog function",
    "localtime": "SQL datetime keyword; not a pg_proc function",
    "localtimestamp": "SQL datetime keyword; not a pg_proc function",
    "array": "ARRAY[...] / ARRAY(SELECT ...) constructor; SQL syntax",
    "row": "ROW(...) constructor; SQL syntax",
    "trim": "TRIM(... FROM ...) syntax; implemented by btrim/ltrim/rtrim",
    # --- sqlglot canonical renames of real read-only PG functions ---------- #
    "timestamp_trunc": "sqlglot's canonical name for date_trunc()",
    "group_concat": "sqlglot's canonical name for string_agg()",
    "rand": "sqlglot's canonical name for random()",
    "time_to_str": "sqlglot's canonical name for to_char()",
    "str_to_date": "sqlglot's canonical name for to_date()",
    "unix_to_time": "sqlglot's canonical name for to_timestamp()",
    "str_position": "sqlglot's canonical name for position()/strpos()",
    "pad": "sqlglot's canonical name for lpad()/rpad()",
    "hex": "sqlglot's canonical name for to_hex()",
    "array_size": "sqlglot's canonical name for array_length()",
    "array_concat": "sqlglot's canonical name for array_cat()",
    "explode": "sqlglot's canonical name for unnest() in the SELECT list",
    "exploding_generate_series": "sqlglot's canonical name for generate_series()",
    "logical_and": "sqlglot's canonical name for bool_and()",
    "logical_or": "sqlglot's canonical name for bool_or()",
    "bitwise_and_agg": "sqlglot's canonical name for bit_and()",
    "bitwise_or_agg": "sqlglot's canonical name for bit_or()",
    "bitwise_xor_agg": "sqlglot's canonical name for bit_xor()",
    "variance_pop": "sqlglot's canonical name for var_pop()",
    "time_from_parts": "sqlglot's canonical name for make_time()",
    "timestamp_from_parts": "sqlglot's canonical name for make_timestamp()",
    "j_s_o_n_array_agg": "sqlglot's canonical name for json_agg()",
    "j_s_o_n_object_agg": "sqlglot's canonical name for json_object_agg()",
    "j_s_o_n_b_object_agg": "sqlglot's canonical name for jsonb_object_agg()",
    "json_extract": "sqlglot's name for json_extract_path() / the -> operator",
    "json_extract_scalar": "sqlglot's name for json_extract_path_text() / the ->> operator",
    "jsonb_extract": "sqlglot's name for the jsonb -> / #> accessor operator",
    "jsonb_extract_scalar": "sqlglot's name for the jsonb ->> / #>> accessor operator",
    # --- sqlglot names for Postgres OPERATORS (containment / path / regex) -- #
    "array_contains": "sqlglot's name for the @> array-contains operator",
    "array_contains_all": "sqlglot's name for the array @> (contains all) operator",
    "array_contained_by": "sqlglot's name for the <@ operator",
    "array_overlaps": "sqlglot's name for the && array-overlap operator",
    "json_array_contains": "sqlglot's name for a json containment operator",
    "j_s_o_n_b_contains_top_key": "sqlglot's name for the jsonb ? operator",
    "j_s_o_n_b_contains_all_top_keys": "sqlglot's name for the jsonb ?& operator",
    "j_s_o_n_b_contains_any_top_keys": "sqlglot's name for the jsonb ?| operator",
    "j_s_o_n_b_path_exists": "sqlglot's name for the jsonb @? operator",
    "j_s_o_n_b_delete_at_path": "sqlglot's name for the jsonb #- operator (pure value transform)",
    "regexp_full_match": "sqlglot's name for the ~ regex-match operator",
    "regexp_i_like": "sqlglot's name for the ~* case-insensitive regex operator",
}


async def _catalog_rows(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """proname + volatility for every allow-listed name present in pg_proc."""
    return await conn.fetch(
        "SELECT proname, provolatile::text AS vol FROM pg_proc WHERE proname = ANY($1::text[])",
        sorted(ALLOWED_FUNCTIONS),
    )


async def test_volatile_functions_are_all_reviewed(reader_conn: asyncpg.Connection) -> None:
    rows = await _catalog_rows(reader_conn)
    volatile = {r["proname"] for r in rows if r["vol"] == "v"}
    unreviewed = sorted(volatile - set(REVIEWED_VOLATILE))
    stale = sorted(set(REVIEWED_VOLATILE) - volatile)
    assert volatile == set(REVIEWED_VOLATILE), (
        "allow-listed functions with a volatile signature do not match REVIEWED_VOLATILE.\n"
        f"  newly volatile / unreviewed (review for side effects!): {unreviewed}\n"
        f"  reviewed but no longer volatile in this PG: {stale}"
    )


async def test_names_absent_from_pg_proc_are_internal(reader_conn: asyncpg.Connection) -> None:
    rows = await _catalog_rows(reader_conn)
    found = {r["proname"] for r in rows}
    not_found = set(ALLOWED_FUNCTIONS) - found
    typos = sorted(not_found - set(SQLGLOT_INTERNAL_NAMES))
    now_found = sorted(set(SQLGLOT_INTERNAL_NAMES) - not_found)
    assert not_found == set(SQLGLOT_INTERNAL_NAMES), (
        "names not in pg_proc must be sqlglot-internal names or SQL operators/keywords.\n"
        f"  unexpected / possible typos: {typos}\n"
        f"  listed as internal but actually in pg_proc now: {now_found}"
    )


async def test_every_name_is_reviewed_exactly_once(reader_conn: asyncpg.Connection) -> None:
    # Belt-and-braces: every allow-listed name is either found in pg_proc or declared
    # internal, and the two review sets never overlap.
    rows = await _catalog_rows(reader_conn)
    found = {r["proname"] for r in rows}
    assert set(SQLGLOT_INTERNAL_NAMES).isdisjoint(found), (
        "a SQLGLOT_INTERNAL_NAMES entry is actually a real pg_proc function: "
        f"{sorted(set(SQLGLOT_INTERNAL_NAMES) & found)}"
    )
    accounted = found | set(SQLGLOT_INTERNAL_NAMES)
    assert accounted == set(ALLOWED_FUNCTIONS), (
        f"unaccounted names: {sorted(set(ALLOWED_FUNCTIONS) - accounted)}"
    )
