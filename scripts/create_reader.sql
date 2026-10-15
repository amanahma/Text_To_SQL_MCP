-- Create/refresh the read-only role used by QueryGuard, for ONE database.
--
-- Run as a superuser once per BIRD database (create_reader.py loops over them all).
-- It operates on the database psql is currently connected to, via the built-in
-- psql variable :"DBNAME". The role name and password come from psql variables:
--   -v reader_user=... -v reader_password=...
--   :"reader_user"     -> quoted identifier   (e.g. "qg_reader")
--   :'reader_user'     -> quoted string literal
--   :'reader_password' -> quoted string literal
--
-- Idempotent: safe to run repeatedly, on every database.

\set ON_ERROR_STOP on

-- 1. Create the login role if it does not exist yet (cluster-global; runs once).
SELECT 'CREATE ROLE ' || quote_ident(:'reader_user') || ' LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'reader_user')
\gexec

-- 2. Pin its attributes and (re)set the password. Least privilege: login, nothing else.
ALTER ROLE :"reader_user" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'reader_password';

-- 3. Database-level access: strip PUBLIC's defaults (CONNECT + TEMPORARY) and grant
--    only CONNECT to the reader. TEMPORARY is intentionally never granted.
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO :"reader_user";

-- 4. For every non-system schema: USAGE + SELECT on existing tables for the reader,
--    and remove CREATE from PUBLIC so nobody can add objects.
SELECT 'GRANT USAGE ON SCHEMA ' || quote_ident(nspname) || ' TO ' || quote_ident(:'reader_user')
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
  AND nspname NOT LIKE 'pg_toast%'
  AND nspname NOT LIKE 'pg_temp%'
\gexec

SELECT 'GRANT SELECT ON ALL TABLES IN SCHEMA ' || quote_ident(nspname) || ' TO ' || quote_ident(:'reader_user')
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
  AND nspname NOT LIKE 'pg_toast%'
  AND nspname NOT LIKE 'pg_temp%'
\gexec

SELECT 'REVOKE CREATE ON SCHEMA ' || quote_ident(nspname) || ' FROM PUBLIC'
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
  AND nspname NOT LIKE 'pg_toast%'
  AND nspname NOT LIKE 'pg_temp%'
\gexec

-- 5. Default privileges: future tables created by postgres (the owner after the
--    split) in each non-system schema are automatically SELECT-able by the reader.
SELECT 'ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA ' || quote_ident(nspname)
       || ' GRANT SELECT ON TABLES TO ' || quote_ident(:'reader_user')
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
  AND nspname NOT LIKE 'pg_toast%'
  AND nspname NOT LIKE 'pg_temp%'
\gexec

-- 6. Make every session this role opens read-only and time-bounded (cluster-global).
ALTER ROLE :"reader_user" SET default_transaction_read_only = on;
ALTER ROLE :"reader_user" SET statement_timeout = '30s';

-- 7. Defence in depth for the Phase 2B red-team findings: the read-only transaction
--    did NOT stop these functions (they write WAL / assign an XID / change session
--    state), so revoke EXECUTE from PUBLIC on EVERY signature of each. check_sql's
--    R6 allow-list already blocks them; this makes the database refuse them too.
--    Idempotent: REVOKE on an already-revoked function is a no-op. The pg_proc loop
--    catches all overloads (e.g. pg_logical_emit_message has text and bytea forms).
SELECT 'REVOKE EXECUTE ON FUNCTION pg_catalog.' || quote_ident(p.proname)
       || '(' || pg_get_function_identity_arguments(p.oid) || ') FROM PUBLIC'
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'pg_catalog'
  AND p.proname IN (
    'pg_logical_emit_message',
    'txid_current',
    'pg_current_xact_id',
    'setseed'
  )
\gexec
