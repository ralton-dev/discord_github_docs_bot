# Deploying a gitdoc instance

End-to-end procedure to bring up a brand-new gitdoc instance on the homelab
Kubernetes cluster, from a clean state to a working `/ask` Discord command.

This is the "I'm doing this for the first time" guide. Once it works,
`implementation_plans/05-operational-runbook.md` will hold the steady-state
ops procedures (rollouts, secret rotation, force-reingest, etc.).

---

## What you need before you start

- `kubectl` configured against the homelab cluster, with permission to
  create namespaces, deployments, jobs, configmaps, and secrets.
- `helm` v3.x.
- `docker` with `buildx` configured (`docker buildx create --use` once).
- Push access to the chosen registry — for MVP that is GHCR under
  `ghcr.io/ralton-dev`. Log in once: `docker login ghcr.io -u <user> -p <pat>`
  using a PAT with `write:packages`.
- Postgres superuser credentials for the homelab Postgres, with `pgvector`
  available (see `db/provision/README.md` for the prerequisite check).
- The target Discord application's bot token, the bot already invited to
  the target guild with `applications.commands` + send-message scope, and
  the guild ID.
- A LiteLLM API key valid for both:
  - the embedding alias `text-embedding-3-small` (or whatever you set in
    `models.embed`), and
  - the chat alias `ollama_chat/llama3.2:3b` (default) — or whichever chat
    alias you override in the per-instance values file.

> **LiteLLM gotcha — verify aliases first.** Hit the LiteLLM `/v1/models`
> endpoint with your key and confirm both aliases above are present. If
> they are not, fix the LiteLLM proxy config before deploying — otherwise
> ingestion will fail at the first embeddings call and `/ask` at the first
> chat completion.

---

## 1. Build and push images

From the repo root:

```sh
docker login ghcr.io -u <github-user> -p <pat-with-write-packages>

# Multi-arch (amd64 + arm64). Tag is auto-derived as <VERSION>-<short-sha>.
make buildx-push REGISTRY=ghcr.io/ralton-dev

# Capture the resolved tag for the values file in step 3.
make print-tag
# -> e.g. 0.1.0-9b83072
```

Three images get pushed:

```
ghcr.io/ralton-dev/gitdoc-discord-bot:<tag>
ghcr.io/ralton-dev/gitdoc-rag:<tag>
ghcr.io/ralton-dev/gitdoc-ingestion:<tag>
```

If any image is private on GHCR, also create a `regcred` secret in the
target namespace per `deploy/REGISTRY.md` and patch the namespace's
`default` ServiceAccount.

---

## 2. Provision Postgres

Pick a slug — lowercase letters, digits, underscores only. The slug is the
identity of the instance: it shows up in the Helm release name, the
namespace, the Postgres role/database, and the `repo` column of `chunks`.
Changing it later means re-ingesting from scratch.

Generate a password and run the provisioning SQL:

```sh
export SLUG=project_a
export INSTANCE_PW="$(openssl rand -base64 32 | tr -d '/+=' | head -c 40)"
export PGPASSWORD='<homelab postgres superuser password>'

psql "postgresql://<superuser>@<homelab-postgres-host>:5432/postgres" \
  -v ON_ERROR_STOP=1 \
  -v slug="$SLUG" \
  -v password="$INSTANCE_PW" \
  -f db/provision/provision-instance.sql
```

This creates:

- role `gitdoc_<slug>` (least privilege)
- database `gitdoc_<slug>` (owned by the role)
- `vector` extension installed in that database

Verify per the checklist in `db/provision/README.md` step 5.

Construct the DSN — you'll paste it into the values file in the next step:

```
postgresql://gitdoc_<slug>:<password>@<homelab-postgres-host>:5432/gitdoc_<slug>?sslmode=require
```

URL-encode the password if it contains anything exotic.

---

## 3. Create the per-instance values file

```sh
cd deploy/helm/gitdoc
cp values-instance.yaml.template values-${SLUG}.yaml
$EDITOR values-${SLUG}.yaml
```

Replace every `CHANGE_ME_*` placeholder. Specifically:

- `repo.name` — your slug (must match the one used in step 2).
- `repo.url` / `repo.branch` — the GitHub repo to index.
- `images.*.tag` — the tag you captured from `make print-tag` in step 1.
- `secrets.discordBotToken` — bot token from the Discord developer portal.
- `secrets.litellmApiKey` — LiteLLM key.
- `secrets.postgresDsn` — the DSN you built in step 2.
- `secrets.gitToken` — only if the repo is private; else leave `""`.
- `discordBot.guildAllowlist` — the Discord guild ID(s), comma-separated.
- `models.chat` — leave the default `ollama_chat/llama3.2:3b` unless you
  know you want something else for THIS instance. (Plan 17 will let
  operators override this at runtime via `/model set`.)
- `ingestion.schedule` — leave `0 */6 * * *` unless your repo changes
  fast enough to warrant tighter cadence.

> **Plaintext-secrets warning.** `values-<slug>.yaml` will contain a real
> Discord token, a real LiteLLM key, and a real Postgres DSN. The repo's
> `.gitignore` already excludes `values-*.yaml` (except `values-example.yaml`).
> Do not commit. Plan 10 (secrets hardening) replaces this with sealed-
> secrets / external-secrets — track that work there, do not roll a custom
> secrets solution here.

---

## 4. Install the chart

```sh
# From the repo root.
make helm-install REPO=${SLUG}
# Equivalent to:
#   helm upgrade --install gitdoc-${SLUG} deploy/helm/gitdoc \
#     --namespace gitdoc-${SLUG} --create-namespace \
#     -f deploy/helm/gitdoc/values-${SLUG}.yaml
```

What happens, in order:

1. Helm creates the namespace `gitdoc-<slug>`.
2. Pre-install hook: the `db-migrate` Job runs `init.sql` against the DSN
   you provided. This creates `chunks` and `ingest_runs`.
3. Main resources land:
   - ConfigMaps (`<release>-config`, `<release>-migrations`)
   - Secret  (`<release>-secrets`)
   - Deployment `<release>-bot`     — Discord gateway client
   - Deployment `<release>-rag`     — FastAPI orchestrator (replicas: 2)
   - Service    `<release>-rag`      — ClusterIP :8000
   - CronJob   `<release>-ingest`   — every 6h by default

Watch it come up:

```sh
kubectl -n gitdoc-${SLUG} get pods -w
kubectl -n gitdoc-${SLUG} get jobs   # confirms db-migrate completed
```

Healthy state:

- `<release>-bot-...`   — `Running 1/1`
- `<release>-rag-...`   — `Running 1/1` x2 (readiness probe waits for the DSN)
- `<release>-db-migrate-...` — `Completed`

---

## 5. Run the smoke test

```sh
NAMESPACE=gitdoc-${SLUG} RELEASE=gitdoc-${SLUG} ./scripts/smoke-test.sh
```

The script:

1. Waits for every chart pod to be `Ready`.
2. Triggers an ad-hoc ingestion: `kubectl create job --from=cronjob/<release>-ingest`.
3. Waits for that Job to `Complete` (default 30 min — large repos may need more).
4. Port-forwards the rag service and POSTs `/ask` with a known question.
5. Asserts the response has a non-empty `answer` and at least one citation.
6. Exec's into the rag pod and counts `chunks WHERE repo=<slug>` to confirm
   the row landed in Postgres.
7. Verifies the latest `ingest_runs` row for the repo has `status='ok'`.

A clean pass ends with `SMOKE TEST PASSED`. Any failure prints diagnostic
output (pod list, Job description, response body) and exits with a
specific code (see header of `scripts/smoke-test.sh`).

---

## 6. Wait for or trigger a real ingestion

The smoke test already triggered one ingestion. The CronJob will fire on
its schedule (`0 */6 * * *` by default) without further intervention. To
re-trigger manually any time:

```sh
kubectl -n gitdoc-${SLUG} create job \
  --from=cronjob/gitdoc-${SLUG}-ingest \
  gitdoc-${SLUG}-ingest-manual-$(date +%s)
```

Tail the resulting Job:

```sh
kubectl -n gitdoc-${SLUG} logs -f -l job-name=gitdoc-${SLUG}-ingest-manual-<ts>
```

Successful tail looks like:

```
INFO gitdoc.ingest cloned <repo>@<sha>
INFO gitdoc.ingest upserted 64 chunks (total=64)
...
INFO gitdoc.ingest garbage-collected <N> old chunks
```

---

## 7. Verify `/ask` from Discord

In the guild listed in `discordBot.guildAllowlist`:

```
/ask  query: what does this project do?
```

Expected response:

- A grounded answer in 1-3 paragraphs.
- A `**Sources**` block listing 1+ ` `-quoted file paths with their
  short commit SHAs.

Sanity-check three more questions whose answers you already know to
confirm groundedness — pick things specific enough that a bare LLM
without retrieval would not get right.

If it looks good: deployment is done.

---

## Rollback

The chart is a single Helm release per instance, so rollback is two steps.

### Just back out the chart (keep the data)

```sh
helm uninstall gitdoc-${SLUG} -n gitdoc-${SLUG}
kubectl delete namespace gitdoc-${SLUG}
```

The Postgres database/role survive — re-running `make helm-install` later
will reuse them. The `db-migrate` Job is idempotent (`CREATE TABLE IF NOT
EXISTS`).

### Full teardown (destructive — drops the database)

```sh
helm uninstall gitdoc-${SLUG} -n gitdoc-${SLUG}
kubectl delete namespace gitdoc-${SLUG}

psql "postgresql://<superuser>@<host>:5432/postgres" \
  -v ON_ERROR_STOP=1 -v slug="$SLUG" \
  -f db/provision/revoke-instance.sql
```

This drops the database (all chunks and ingest_runs gone) and the role.
Take a `pg_dump` of `gitdoc_<slug>` first if there is any chance you want
the embeddings back.

---

## Troubleshooting

### `ImagePullBackOff` / `ErrImagePull`

- Confirm the tag in `images.*.tag` matches an image actually pushed to
  GHCR. `gh api /users/ralton-dev/packages/container/gitdoc-rag/versions`
  lists what's there. Re-run `make buildx-push` if you forgot.
- If GHCR packages are private, you need a `regcred` secret in the
  namespace and the `default` SA patched — see `deploy/REGISTRY.md`.
- Multi-arch: confirm the cluster's nodes are amd64 or arm64 (not
  something exotic) — `kubectl get nodes -o wide` and check `OS-IMAGE` /
  `KERNEL-VERSION`.

### `db-migrate` Job fails

- Most common: `secrets.postgresDsn` is wrong or the DB doesn't exist.
  `kubectl -n gitdoc-${SLUG} logs job/gitdoc-${SLUG}-db-migrate`.
  Re-run `db/provision/provision-instance.sql` if needed.
- `ERROR: extension "vector" is not available`: pgvector isn't installed
  on the Postgres server. Install the OS package
  (`postgresql-16-pgvector`) and retry.
- TLS errors (`SSL connection has been closed`): the server doesn't
  present TLS — switch the DSN suffix from `?sslmode=require` to
  `?sslmode=prefer`.

### Bot pod `CrashLoopBackOff`

- `kubectl -n gitdoc-${SLUG} logs deploy/gitdoc-${SLUG}-bot`.
- `LoginFailure` / `Improper token`: bot token is wrong or revoked.
- `KeyError: 'RAG_ORCHESTRATOR_URL'`: ConfigMap missing — check
  `kubectl get cm gitdoc-${SLUG}-config -o yaml`. Should never happen
  with the default chart.

### RAG pod `0/1 Ready` — readiness probe failing

- The `/readyz` endpoint actively connects to Postgres. A failure means
  the DSN is unreachable from the pod.
- `kubectl -n gitdoc-${SLUG} exec deploy/gitdoc-${SLUG}-rag -- python -c \
   "import os, psycopg; psycopg.connect(os.environ['POSTGRES_DSN'], connect_timeout=5)"`
  — look at the exception.
- Common: NetworkPolicy blocking egress to the homelab Postgres host;
  Postgres host not resolvable from inside the cluster (CoreDNS),
  password URL-encoding issue.

### Ingestion runs but `/ask` returns "I couldn't find anything relevant"

- Likely `embedDim` mismatch. The `chunks.embedding` column type is set
  at install time from `models.embedDim` (default 1536). If you later
  change `models.embed` to a different model with a different dimension
  (e.g. `nomic-embed-text` is 768), every new embedding fails to insert
  with `cannot cast type vector(N) to vector(M)`.
  - Fix: pick the embedding model up front. To switch, drop the database,
    re-run provisioning, change `models.embed` AND `models.embedDim`,
    helm upgrade, re-ingest.
- Or: the repo column key doesn't match. Both ingestion (`REPO_NAME` env)
  and the bot (`TARGET_REPO` env) are derived from `repo.name` in values
  — they must agree. If you renamed the slug between deploys, old chunks
  won't match.

### `/ask` 502 from the bot ("Something went wrong reaching the knowledge base")

- `kubectl -n gitdoc-${SLUG} logs deploy/gitdoc-${SLUG}-rag` — usually
  one of:
  - `embedding backend unavailable` — LiteLLM unreachable, key invalid,
    or model alias missing.
  - `chat backend unavailable` — same as above for the chat model alias.
- Confirm aliases: from a debug pod, `curl
  -H 'authorization: Bearer <key>' $LITELLM_BASE_URL/v1/models | jq` and
  look for both `text-embedding-3-small` and `ollama_chat/llama3.2:3b`.

### Pod-level `securityContext` rejecting the image

- Symptom: `container has runAsNonRoot and image has non-numeric user ...`
- The chart sets `runAsUser: 10001` explicitly. If you build a custom
  image that uses a different uid, override `securityContext.runAsUser`
  via `values-<slug>.yaml`. The bundled images all use uid 10001 for
  the app and 70 for the postgres-alpine migration image.

---

## What this guide intentionally does not cover

- Routine rollouts of a new image version (covered by plan 05).
- Rotating Discord/LiteLLM/Postgres credentials (plan 05).
- Backup/restore of the embeddings (parking lot in working-memory.md).
- Webhook-driven ingestion instead of CronJob (plan 15).
- Anything secrets-management beyond "stick the DSN in a values file"
  (plan 10).
