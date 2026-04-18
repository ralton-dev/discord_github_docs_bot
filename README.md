# gitdoc

A Discord slash-command bot that answers questions about a specific software
project by retrieving grounded context from that project's own code and
documentation. One bot instance per repo, deployed to Kubernetes via Helm.

Users type `/ask <question>` in Discord; the bot retrieves the most relevant
chunks from a pgvector database, pipes them through an LLM via LiteLLM, and
replies with an answer plus file citations (`path` at `commit_sha`). Replies
in the resulting thread are treated as follow-ups with the prior turns
included as chat history.

## What's in the box

- **Hybrid retrieval** — pgvector HNSW cosine similarity fused with Postgres
  full-text search (BM25-style) via Reciprocal Rank Fusion, so exact
  identifier lookups (`process_batch`, error strings) land the right chunk.
  Optional cross-encoder reranker slots in before the LLM call.
- **Query cache** — identical queries against the same commit SHA return
  instantly, with natural invalidation on every new ingestion.
- **Webhook-driven ingestion** — `/webhook` accepts signed GitHub/GitLab push
  events and spawns a Kubernetes Job from the ingestion CronJob. The cron
  remains as a safety net.
- **Runtime model selection** — `/model list` / `/model current` /
  `/model set` slash commands change the active chat model per-instance
  with no redeploy. Gated on Discord's built-in Manage Server permission.
- **Per-guild and per-user token rate limits** — rolling 1h window,
  friendly 429 response surfaced by the bot.
- **Observability** — Prometheus `/metrics` endpoint on the orchestrator,
  structured JSON logs from all three services, optional `ServiceMonitor`
  when the Prometheus Operator is installed.
- **Sealed-secrets flow** — `secrets.existingSecret` gate lets operators
  ship credentials via `kubeseal` instead of a plaintext values file.
- **CI/CD** — GitHub Actions pipeline: lint + unit + Helm tests on every PR,
  multi-arch (amd64/arm64) image publish to GHCR on every `v*` tag.

## Architecture

Three stateless services, one Postgres:

```
                      ┌──────────────────────┐
                      │   Discord gateway    │
                      └──────────┬───────────┘
                                 │  /ask, /model, threads
                  ┌──────────────▼──────────────┐
                  │    services/discord-bot     │
                  └──────────────┬──────────────┘
                                 │  HTTP /ask
                  ┌──────────────▼──────────────┐
                  │       services/rag          │────► Prometheus /metrics
                  │  (FastAPI + pgvector query) │
                  └──┬──────────────────────┬──┘
                     │                      │
                     │ embed + chat         │ sql
                     ▼                      ▼
             ┌──────────────┐      ┌──────────────────┐
             │   LiteLLM    │      │     Postgres     │
             │  (OpenAI-    │      │ + pgvector + FTS │
             │   compatible)│      └────────▲─────────┘
             └──────────────┘               │
                                            │ chunks + embeddings
                          ┌─────────────────┴─────────────────┐
                          │      services/ingestion           │
                          │  (CronJob + webhook-spawned Job)  │
                          └───────────────────────────────────┘
```

Each Helm release runs one repo: its own namespace, its own Postgres
role/db, its own Discord bot token. Scale out by spinning up more
releases, not more replicas — Discord enforces one gateway session per
bot token.

## Quick start (local)

```sh
cp .env.example .env              # fill LITELLM_BASE_URL + API key
docker compose up -d db rag
docker compose run --rm ingest    # seeds the DB from REPO_URL
curl -XPOST localhost:8000/ask \
  -H 'content-type: application/json' \
  -d '{"query":"what is this project","repo":"self"}'
```

See `docker-compose.yml` for the LiteLLM wiring options (SSH tunnel or
bring-your-own mock).

## Deploying a new instance

End-to-end walk-through lives in [`deploy/DEPLOY.md`](deploy/DEPLOY.md):

1. `git tag vX.Y.Z && git push origin vX.Y.Z` — CI publishes images to
   `ghcr.io/ralton-dev/gitdoc-{bot,rag,ingestion}`.
2. One-time per instance: run `db/provision/provision-instance.sql` against
   the homelab Postgres as superuser (see
   [`db/provision/README.md`](db/provision/README.md)).
3. Seal a Secret with `kubeseal` and commit it to `deploy/sealed-secrets/`
   (see [`deploy/SECRETS.md`](deploy/SECRETS.md)), or use the bootstrap
   plaintext path for the first deploy.
4. Copy `deploy/helm/gitdoc/values-instance.yaml.template` to
   `values-<slug>.yaml` and fill in the `CHANGE_ME_*` fields.
5. `make helm-install REPO=<slug>`.
6. `NAMESPACE=gitdoc-<slug> RELEASE=gitdoc-<slug> ./scripts/smoke-test.sh`.
7. Enable Discord's Message Content Intent in the developer portal
   (required for thread follow-ups).

## Documentation

- [`deploy/DEPLOY.md`](deploy/DEPLOY.md) — full deploy procedure
- [`deploy/SECRETS.md`](deploy/SECRETS.md) — sealed-secrets workflow
- [`deploy/REGISTRY.md`](deploy/REGISTRY.md) — image registry setup
- [`docs/retrieval.md`](docs/retrieval.md) — hybrid search, reranker, cache
- [`docs/webhooks.md`](docs/webhooks.md) — GitHub/GitLab push webhooks
- [`docs/observability.md`](docs/observability.md) — metrics + logs
- [`docs/models.md`](docs/models.md) — runtime model selection
- [`db/provision/README.md`](db/provision/README.md) — Postgres provisioning
- [`implementation_plans/`](implementation_plans/) — design decisions and
  open work (the runbook lives here until the first deploy has validated
  it in practice)

## Tests

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt \
            -r services/ingestion/requirements.txt \
            -r services/discord-bot/requirements.txt \
            -r services/rag/requirements.txt
make test                # unit tests (~1s)
make integration-test    # end-to-end with testcontainers (~3s warm)
```

## Releasing

```sh
make bump-version VERSION=x.y.z   # rewrites Chart.yaml + values defaults, tags
git push origin main
git push origin vx.y.z            # triggers release.yml on CI
```

## License

MIT — see [LICENSE](LICENSE).
