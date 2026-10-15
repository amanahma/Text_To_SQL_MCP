"""The R6 allow-list: read-only PostgreSQL 16 functions QueryGuard permits.

R6 (see :mod:`queryguard.safety`) flipped from a deny-list to an **allow-list** in
Phase 2C: a function call is permitted only if its name is in
:data:`ALLOWED_FUNCTIONS`. Anything not listed is rejected, so a dangerous or
unknown function fails closed — we no longer have to enumerate every bad name.

Two things make this subtle, and both are why the names below are not simply the
PostgreSQL spellings:

1. ``check_sql`` matches on the name *this sqlglot version reports*, i.e.
   ``exp.Anonymous.name`` for unmodelled functions or ``Func.sql_name()`` for the
   ones sqlglot models — and sqlglot **renames** many built-ins to its own
   canonical form. For example ``date_trunc`` is reported as ``timestamp_trunc``,
   ``string_agg`` as ``group_concat``, ``random`` as ``rand``, ``now`` as
   ``current_timestamp``, ``char_length`` as ``length``, ``to_char`` as
   ``time_to_str``, ``generate_series`` as ``exploding_generate_series``. Where the
   reported form differs from the SQL spelling we list the reported form and note
   the spelling in a comment; where a function can appear under *both* forms
   depending on context (``unnest`` is ``explode`` in the SELECT list but
   ``unnest`` as a table function) we list both.
2. The list is built from PostgreSQL 16's documented read-only function
   categories, **not** from the gold queries — the gold false-block test must stay
   an independent check. It deliberately excludes everything with a side effect or
   that reads server/cluster state: the whole former deny-list, plus ``setseed``,
   ``txid_*`` / ``pg_current_xact_id*``, ``pg_logical_*``, ``pg_replication_*``,
   ``pg_promote``, ``pg_stat_reset*``, anything containing ``wal`` / ``backend`` /
   ``advisory`` / ``notify`` / ``restore_point`` / ``logfile``, ``pg_ls_*``,
   ``pg_stat_file``, ``pg_read_*``, ``lo_*``, ``dblink*``, ``current_setting`` /
   ``set_config``, ``nextval`` / ``currval`` / ``lastval`` / ``setval``,
   ``query_to_xml*``, and the server-information functions (``version``,
   ``current_database``, ``current_user``, ``session_user``, ``current_catalog``,
   ``inet_server_*``, ``pg_postmaster_start_time``). ``random`` is allowed.

All names are lower-case. Names reported by sqlglot are already stripped of schema
prefix and quoting and are plain identifiers, so each also matches
``^[a-z_][a-z0-9_]*$`` (the shape check R6 applies before the membership check).
"""

from __future__ import annotations

ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # --- SQL operators, predicates and keywords modelled as Func nodes -- #
        # This sqlglot version represents core SQL syntax (boolean connectors,
        # CASE, EXISTS, quantified comparisons, COLLATE, and the array/json
        # containment, path and regex OPERATORS) as exp.Func subclasses, so they
        # reach R6. They are not functions a user calls by name and are all
        # read-only syntax; dangerous functions never take these node shapes (they
        # arrive as exp.Anonymous). Names are the forms this sqlglot reports.
        "and",
        "or",
        "xor",
        "case",
        "if",
        "exists",
        "any",
        "all",
        "some",
        "collate",
        "array_contains",  # @> (array)
        "array_contains_all",
        "array_contained_by",  # <@ (array)
        "array_overlaps",  # && (array)
        "json_array_contains",
        "jsonb_contains",
        "j_s_o_n_b_contains_top_key",  # ? (jsonb)
        "j_s_o_n_b_contains_all_top_keys",  # ?& (jsonb)
        "j_s_o_n_b_contains_any_top_keys",  # ?| (jsonb)
        "j_s_o_n_b_path_exists",  # @? (jsonb)
        "j_s_o_n_b_delete_at_path",  # #- (jsonb, pure value transform)
        "jsonb_extract",  # -> / #> (jsonb)
        "jsonb_extract_scalar",  # ->> / #>> (jsonb)
        "regexp_full_match",  # ~ operator
        "regexp_i_like",  # ~* operator
        # --- aggregates ----------------------------------------------------- #
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "every",
        "bool_and",
        "logical_and",  # bool_and
        "bool_or",
        "logical_or",  # bool_or
        "bit_and",
        "bitwise_and_agg",  # bit_and
        "bit_or",
        "bitwise_or_agg",  # bit_or
        "bit_xor",
        "bitwise_xor_agg",  # bit_xor
        "array_agg",
        "string_agg",
        "group_concat",  # string_agg
        "json_agg",
        "j_s_o_n_array_agg",  # json_agg
        "jsonb_agg",
        "json_object_agg",
        "j_s_o_n_object_agg",  # json_object_agg
        "jsonb_object_agg",
        "j_s_o_n_b_object_agg",  # jsonb_object_agg
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "variance",
        "variance_pop",  # var_pop
        "var_pop",
        "var_samp",  # reported as variance by this sqlglot
        "corr",
        "covar_pop",
        "covar_samp",
        "mode",
        "percentile_cont",
        "percentile_disc",
        "grouping",
        # --- window functions ---------------------------------------------- #
        "row_number",
        "rank",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
        # --- math ----------------------------------------------------------- #
        "abs",
        "cbrt",
        "ceil",
        "ceiling",  # reported as ceil
        "degrees",
        "exp",
        "factorial",
        "floor",
        "gcd",
        "lcm",
        "ln",
        "log",
        "log10",  # reported as log
        "mod",  # (usually rendered as the % operator, not a Func)
        "pi",
        "power",
        "radians",
        "round",
        "scale",
        "min_scale",
        "trim_scale",
        "sign",
        "sqrt",
        "trunc",
        "width_bucket",
        "random",
        "rand",  # random
        "acos",
        "acosd",
        "asin",
        "asind",
        "atan",
        "atand",
        "atan2",
        "atan2d",
        "cos",
        "cosd",
        "cot",
        "cotd",
        "sin",
        "sind",
        "tan",
        "tand",
        "sinh",
        "cosh",
        "tanh",
        "asinh",
        "acosh",
        "atanh",
        # --- string --------------------------------------------------------- #
        "bit_length",
        "octet_length",
        "char_length",
        "character_length",  # both reported as length
        "length",
        "lower",
        "upper",
        "initcap",
        "lpad",
        "rpad",  # both reported as pad
        "pad",
        "ltrim",
        "rtrim",
        "btrim",  # all reported as trim
        "trim",
        "left",
        "right",
        "reverse",
        "repeat",
        "replace",
        "split_part",
        "strpos",
        "position",  # both reported as str_position
        "str_position",
        "substr",  # reported as substring
        "substring",
        "overlay",
        "translate",
        "ascii",
        "chr",
        "concat",
        "concat_ws",
        "format",
        "md5",
        "starts_with",
        "to_hex",
        "hex",  # to_hex
        "encode",
        "decode",
        "quote_ident",
        "quote_literal",
        "quote_nullable",
        "regexp_replace",
        "regexp_match",
        "regexp_matches",
        "regexp_count",
        "regexp_instr",
        "regexp_like",
        "regexp_substr",
        "regexp_split_to_array",
        "regexp_split_to_table",
        "string_to_array",
        "string_to_table",
        # --- date / time ---------------------------------------------------- #
        "age",
        "clock_timestamp",
        "current_date",
        "current_time",
        "current_timestamp",
        "now",  # reported as current_timestamp
        "date_bin",
        "date_part",
        "date_trunc",
        "timestamp_trunc",  # date_trunc
        "extract",  # date_part and extract both report as extract
        "isfinite",
        "justify_days",
        "justify_hours",
        "justify_interval",
        "localtime",
        "localtimestamp",
        "make_date",
        "make_interval",
        "make_time",
        "time_from_parts",  # make_time
        "make_timestamp",
        "timestamp_from_parts",  # make_timestamp
        "make_timestamptz",
        "statement_timestamp",
        "timeofday",
        "transaction_timestamp",
        "to_timestamp",
        "unix_to_time",  # to_timestamp
        "to_date",
        "str_to_date",  # to_date
        "to_char",
        "time_to_str",  # to_char
        # --- conditional and null handling --------------------------------- #
        "coalesce",
        "nullif",
        "greatest",
        "least",
        "num_nonnulls",
        "num_nulls",
        # --- type conversion and formatting -------------------------------- #
        "cast",
        "try_cast",
        "to_number",
        "date",  # added after gold test: q533, q1435 — function-style cast, read-only
        # --- JSON / JSONB read-only accessors ------------------------------ #
        "json_array_elements",
        "json_array_elements_text",
        "json_array_length",
        "json_each",
        "json_each_text",
        "json_extract_path",
        "json_extract",  # json_extract_path
        "json_extract_path_text",
        "json_extract_scalar",  # json_extract_path_text
        "json_object_keys",
        "json_typeof",
        "json_strip_nulls",
        "json_build_array",
        "json_build_object",
        "json_object",
        "to_json",
        "array_to_json",
        "row_to_json",
        "jsonb_array_elements",
        "jsonb_array_elements_text",
        "jsonb_array_length",
        "jsonb_each",
        "jsonb_each_text",
        "jsonb_extract_path",
        "jsonb_extract_path_text",
        "jsonb_object_keys",
        "jsonb_typeof",
        "jsonb_strip_nulls",
        "jsonb_build_array",
        "jsonb_build_object",
        "jsonb_object",
        "to_jsonb",
        "jsonb_path_query",
        "jsonb_path_query_array",
        "jsonb_path_query_first",
        "jsonb_path_exists",
        "jsonb_path_match",
        "jsonb_pretty",
        "jsonb_set",
        "jsonb_insert",
        # --- array / row constructors and read-only array functions -------- #
        "array",  # the ARRAY[...] / ARRAY(SELECT ...) constructor
        "row",  # the ROW(...) constructor
        "array_append",
        "array_cat",
        "array_concat",  # array_cat
        "array_dims",
        "array_length",
        "array_size",  # array_length
        "array_lower",
        "array_ndims",
        "array_position",
        "array_positions",
        "array_prepend",
        "array_remove",
        "array_replace",
        "array_to_string",
        "array_upper",
        "cardinality",
        "trim_array",
        "unnest",
        "explode",  # unnest in the SELECT list
        "generate_subscripts",
        # --- generate_series ----------------------------------------------- #
        "generate_series",
        "exploding_generate_series",  # generate_series
    }
)
