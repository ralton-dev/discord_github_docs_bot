-- Decommission a gitdoc instance: drop the database and the role.
--
-- Run this as a Postgres superuser against the homelab cluster.
-- Required psql variable:
--   :slug  the instance slug to tear down (same value used when provisioning).
--
-- Example:
--   psql "postgresql://<superuser>@<host>:5432/postgres" \
--     -v ON_ERROR_STOP=1 \
--     -v slug=project_a \
--     -f revoke-instance.sql
--
-- WARNING: this is destructive. DROP DATABASE deletes all chunks and
-- ingest_runs for the instance. Take a backup first if there is any
-- chance you want the data back. Ensure no Helm release is still
-- running against this DB (uninstall the chart first).

\set ON_ERROR_STOP on

-- 1. Terminate any leftover connections from the chart so DROP DATABASE
--    does not fail with "database is being accessed by other users".
--    This is safe: the Helm release should already be uninstalled.
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'gitdoc_' || :'slug'
  AND pid <> pg_backend_pid();

-- 2. Drop the database. IF EXISTS so re-running is a no-op.
SELECT format('DROP DATABASE IF EXISTS %I', 'gitdoc_' || :'slug') \gexec

-- 3. Drop the role. IF EXISTS so re-running is a no-op.
--    The role owned the database, so no other objects should remain once
--    the DB is dropped. If this fails with "role cannot be dropped because
--    some objects depend on it", inspect with \du and REASSIGN/DROP OWNED
--    before retrying.
SELECT format('DROP ROLE IF EXISTS %I', 'gitdoc_' || :'slug') \gexec

\echo
\echo 'Revoked:'
\echo '  database : gitdoc_':slug' (dropped)'
\echo '  role     : gitdoc_':slug' (dropped)'
