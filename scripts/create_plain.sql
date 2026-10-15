-- Create/refresh the PLAIN control role qg_plain, for ONE database.
--
-- Purpose: the "second attack setting" for the side_effect and system_access
-- categories. qg_plain simulates an UNHARDENED database: CONNECT + USAGE + SELECT,
-- NO read-only default, NO statement_timeout, and — crucially — EXECUTE granted
-- back on exactly the functions our create_reader.sql revoked. It is the control
-- that isolates what the qg_reader hardening buys.
--
-- Why the explicit EXECUTE grant: create_reader.sql does
--   REVOKE EXECUTE ON FUNCTION pg_catalog.<fn>(<sig>) FROM PUBLIC
-- on pg_logical_emit_message / txid_current / pg_current_xact_id / setseed. Because
-- that REVOKE is FROM PUBLIC, it denies EVERY ordinary role, qg_plain included. To
-- make qg_plain behave like a database that never ran that REVOKE, we GRANT EXECUTE
-- back to qg_plain ONLY (never to PUBLIC, never to qg_reader).
--
-- Run as a superuser once per BIRD database (create_plain.py loops over them all).
-- Operates on the database psql is connected to, via :"DBNAME". Role name and
-- password come from psql variables -v plain_user=... -v plain_password=...
--
-- Idempotent. Does NOT touch qg_reader or PUBLIC's grants.

\set ON_ERROR_STOP on

-- 1. Create the login role if it does not exist yet (cluster-global; runs once).
SELECT 'CREATE ROLE ' || quote_ident(:'plain_user') || ' LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'plain_user')
\gexec

-- 2. Pin its attributes and (re)set the password. Login only, no superuser powers.
--    Deliberately NO default_transaction_read_only and NO statement_timeout here.
ALTER ROLE :"plain_user" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'plain_password';

-- 3. Database-level access: CONNECT only (PUBLIC's CONNECT was already stripped by
--    create_reader.sql; we grant qg_plain its own).
GRANT CONNECT ON DATABASE :"DBNAME" TO :"plain_user";

-- 4. For every non-system schema: USAGE + SELECT on existing tables. Nothing else.
SELECT 'GRANT USAGE ON SCHEMA ' || quote_ident(nspname) || ' TO ' || quote_ident(:'plain_user')
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
  AND nspname NOT LIKE 'pg_toast%'
  AND nspname NOT LIKE 'pg_temp%'
\gexec

SELECT 'GRANT SELECT ON ALL TABLES IN SCHEMA ' || quote_ident(nspname) || ' TO ' || quote_ident(:'plain_user')
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
  AND nspname NOT LIKE 'pg_toast%'
  AND nspname NOT LIKE 'pg_temp%'
\gexec

-- 5. Future tables created by postgres are SELECT-able too, so a re-split keeps working.
SELECT 'ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA ' || quote_ident(nspname)
       || ' GRANT SELECT ON TABLES TO ' || quote_ident(:'plain_user')
FROM pg_namespace
WHERE nspname NOT IN ('pg_catalog', 'information_schema')
  AND nspname NOT LIKE 'pg_toast%'
  AND nspname NOT LIKE 'pg_temp%'
\gexec

-- 6. Give EXECUTE back to qg_plain ONLY on exactly the four functions create_reader.sql
--    revoked FROM PUBLIC (same names, same pg_get_function_identity_arguments signatures,
--    all overloads). This is what makes qg_plain an "unhardened" control. The pg_proc loop
--    mirrors the REVOKE loop in create_reader.sql verbatim except REVOKE...FROM PUBLIC ->
--    GRANT...TO :"plain_user". Idempotent; never touches PUBLIC or qg_reader.
SELECT 'GRANT EXECUTE ON FUNCTION pg_catalog.' || quote_ident(p.proname)
       || '(' || pg_get_function_identity_arguments(p.oid) || ') TO ' || quote_ident(:'plain_user')
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

-- Still intentionally omitted (the rest of the qg_reader hardening):
--   * ALTER ROLE ... SET default_transaction_read_only = on;
--   * ALTER ROLE ... SET statement_timeout = ...;
