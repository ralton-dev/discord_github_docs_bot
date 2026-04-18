-- Provision a new gitdoc instance: role + database + pgvector extension.
--
-- Run this as a Postgres superuser against the homelab cluster.
-- Required psql variables:
--   :slug      the instance slug (e.g. project_a). Must be a valid Postgres
--              identifier: lowercase letters, digits, underscores only.
--   :password  a strong password for the new role.
--
-- Example:
--   psql "postgresql://<superuser>@<host>:5432/postgres" \
--     -v ON_ERROR_STOP=1 \
--     -v slug=project_a \
--     -v password="$(openssl rand -base64 32 | tr -d '/+=' | head -c 40)" \
--     -f provision-instance.sql
--
-- Re-running this file for an existing slug is a no-op for the role
-- (password is NOT rotated) and will surface a clear error from
-- CREATE DATABASE if the DB already exists — bootstrap only once.

\set ON_ERROR_STOP on

-- 1. Role: create with least-privilege attributes if it doesn't already exist.
--    We use a DO block so the role name can be interpolated safely via
--    format() + %I (identifier) and %L (literal).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'gitdoc_' || :'slug'
  ) THEN
    EXECUTE format(
      'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION PASSWORD %L',
      'gitdoc_' || :'slug',
      :'password'
    );
  ELSE
    RAISE NOTICE 'role % already exists, leaving password unchanged', 'gitdoc_' || :'slug';
  END IF;
END
$$;

-- 2. Database: owned by the new role. Postgres does not support
--    CREATE DATABASE IF NOT EXISTS, so we use psql's \gexec to conditionally
--    emit the CREATE DATABASE statement only when the DB is absent.
--    This keeps the script idempotent for the role step while making
--    the database step a clear "bootstrap once" operation.
SELECT format(
  'CREATE DATABASE %I OWNER %I',
  'gitdoc_' || :'slug',
  'gitdoc_' || :'slug'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = 'gitdoc_' || :'slug'
)
\gexec

-- 3. Grants on the database itself (CONNECT + CREATE schemas).
--    CREATE on DATABASE lets the role create schemas/tables; the Helm
--    db-migrate Job needs this to run init.sql.
SELECT format(
  'GRANT CONNECT, CREATE ON DATABASE %I TO %I',
  'gitdoc_' || :'slug',
  'gitdoc_' || :'slug'
)
\gexec

-- 4. Switch into the new database and install pgvector as superuser.
--    Doing this once here means the Helm db-migrate Job's
--    CREATE EXTENSION IF NOT EXISTS vector becomes a no-op and the
--    unprivileged gitdoc_<slug> role does not need CREATE EXTENSION
--    privileges.
--
--    Compute the target DB name into the psql variable `dbname` so we
--    can \connect to it by identifier.
SELECT 'gitdoc_' || :'slug' AS dbname \gset
\connect :"dbname"

CREATE EXTENSION IF NOT EXISTS vector;

-- 5. Hand the `public` schema to the instance role.
--    Postgres 15+ locked down the default public-schema ACLs: the DB
--    owner role no longer implicitly has CREATE on `public`, so tables
--    created by the db-migrate Job end up under the connecting role only
--    if CREATE is explicit. Transferring ownership of the schema to the
--    instance role is the cleanest fix — this is a dedicated per-instance
--    database, so the role effectively owns everything in it. All
--    subsequent CREATE TABLE / CREATE INDEX statements end up owned by
--    the instance role, which is what every other query path (including
--    ingestion upserts and DROP-on-decommission) assumes.
SELECT format('ALTER SCHEMA public OWNER TO %I', 'gitdoc_' || :'slug')
\gexec
SELECT format('GRANT ALL ON SCHEMA public TO %I', 'gitdoc_' || :'slug')
\gexec

-- 5. Summary for the operator.
\echo
\echo 'Provisioned:'
\echo '  role     : gitdoc_':slug
\echo '  database : gitdoc_':slug
\echo '  extension: vector (installed in the new database)'
\echo
\echo 'Next: construct the DSN and drop it into values-':slug'.yaml#secrets.postgresDsn:'
\echo '  postgresql://gitdoc_':slug':<password>@<host>:5432/gitdoc_':slug'?sslmode=require'
