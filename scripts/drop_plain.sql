-- Remove qg_plain's privileges in ONE database (per-database step of the drop).
--
-- DROP OWNED BY removes every grant/ownership the role holds in the CURRENT
-- database. It must run in every database the role can touch BEFORE the final
-- cluster-global DROP ROLE (drop_plain.py does the DROP ROLE last). Guarded so it
-- is a no-op if the role was never created.
--
-- Takes -v plain_user=... . Idempotent. Never touches qg_reader.

\set ON_ERROR_STOP on

SELECT 'DROP OWNED BY ' || quote_ident(:'plain_user')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'plain_user')
\gexec
