# Postgres instance provisioning

One-time admin procedure to create the dedicated database and role that a
new gitdoc Helm release needs. The Helm `db-migrate` pre-install Job
creates the *tables* (via `db/init.sql`), but the *database* and the
*role* it connects as must already exist — that is what the SQL in this
directory is for.

Naming convention (matches the Helm release slug):

- role: `gitdoc_<slug>`
- database: `gitdoc_<slug>`

## Prerequisites

- psql on the machine running this procedure.
- Superuser (or equivalent `CREATEDB` + `CREATEROLE` + `CREATE EXTENSION`
  capable) credentials for the homelab Postgres.
- **`pgvector` must be available on the server.** The script runs
  `CREATE EXTENSION IF NOT EXISTS vector` inside the new database; this
  fails if the `vector` extension is not installed at the OS / package
  level. On Debian/Ubuntu that's `apt install postgresql-16-pgvector`
  (adjust for your major version); in official Postgres Docker images
  it ships with `pgvector/pgvector:pg16`. Install once per server, not
  per instance.

## 1. Generate a strong password

```sh
openssl rand -base64 32 | tr -d '/+=' | head -c 40
```

Keep the output — you'll need it twice (once for the SQL, once for the
DSN). Do not commit it anywhere.

## 2. Run the provisioning SQL

Pick a slug — must be a valid lowercase Postgres identifier (letters,
digits, underscores). It must match the Helm release name / values file
suffix.

```sh
export SLUG=project_a
export PGPASSWORD='<super user password>'    # or rely on .pgpass
export INSTANCE_PW='<password from step 1>'

psql "postgresql://<superuser>@<homelab-postgres-host>:5432/postgres" \
  -v ON_ERROR_STOP=1 \
  -v slug="$SLUG" \
  -v password="$INSTANCE_PW" \
  -f provision-instance.sql
```

What this does:

1. Creates the role `gitdoc_<slug>` with `LOGIN`, `NOSUPERUSER`,
   `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`, `NOREPLICATION` and the
   supplied password. Safe to re-run; password is *not* rotated on
   re-run.
2. Creates the database `gitdoc_<slug>` owned by that role (only on
   first run — Postgres has no `CREATE DATABASE IF NOT EXISTS`, so
   the script uses a conditional `\gexec` pattern).
3. Grants `CONNECT` and `CREATE` on the database to the new role.
4. Connects into the new database as the superuser and installs the
   `vector` extension. Doing this here means the Helm `db-migrate`
   Job's `CREATE EXTENSION IF NOT EXISTS vector` is a no-op and the
   unprivileged role never needs `CREATE EXTENSION` rights.

## 3. Construct the DSN

```
postgresql://gitdoc_<slug>:<password>@<homelab-postgres-host>:5432/gitdoc_<slug>?sslmode=require
```

- URL-encode the password if it contains anything exotic
  (`python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1], safe=""))' "$INSTANCE_PW"`).
- `sslmode=require` assumes the homelab Postgres presents TLS.
  Downgrade to `sslmode=prefer` only if you have verified it does not.

## 4. Wire it into the Helm values

For MVP, drop the DSN straight into the per-instance values file:

```yaml
# deploy/helm/gitdoc/values-<slug>.yaml
secrets:
  postgresDsn: "postgresql://gitdoc_<slug>:<password>@<host>:5432/gitdoc_<slug>?sslmode=require"
```

This becomes the `POSTGRES_DSN` Secret key the `db-migrate` Job, the
RAG orchestrator, and the ingestion CronJob all read.

**Future home:** once task 10 (secrets hardening) lands, this DSN
should move to sealed-secrets / external-secrets instead of living in
a plaintext values file. Update this README when that flow exists.

## 5. Verify

Reconnect as the new role (not as superuser) and spot-check:

```sh
psql "postgresql://gitdoc_<slug>:<password>@<host>:5432/gitdoc_<slug>?sslmode=require"
```

Inside psql:

```
\du gitdoc_<slug>         -- role exists, no superuser / createdb
\l gitdoc_<slug>          -- DB exists, owner is gitdoc_<slug>
\dx                        -- vector extension is installed
SELECT 1;                  -- basic auth + connect works
```

A clean run should show:

- `\du` — `gitdoc_<slug>` with no `Superuser`, no `Create DB`, no
  `Create role`.
- `\l` — owner `gitdoc_<slug>`.
- `\dx` — `vector` listed.
- `SELECT 1` — returns `1`.

The Helm `db-migrate` pre-install hook will then create the `chunks`
and `ingest_runs` tables the first time the chart is installed.

## Decommissioning

To tear an instance down (after `helm uninstall`):

```sh
psql "postgresql://<superuser>@<host>:5432/postgres" \
  -v ON_ERROR_STOP=1 \
  -v slug="$SLUG" \
  -f revoke-instance.sql
```

This terminates leftover backends, drops the database, and drops the
role. **Destructive** — take a backup first if you might want the
embeddings back.
