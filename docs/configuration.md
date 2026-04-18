# Configuration reference

Every knob that affects a running instance. Most live in `values.yaml`
(overridden per-instance via your ArgoCD Application or
`values-<slug>.yaml`). A few runtime-only settings live in Postgres — the
ones that can be changed without a redeploy.

## Quick lookup — "I want to…"

| What you want | Where to set it |
|---|---|
| Point the bot at a different repo | `repo.url` |
| Follow a different branch (e.g. `master`, `develop`) | `repo.branch` (default `main`) |
| Change the active chat model | Discord: `/model set <name>` (no chart value) |
| Allow the bot in specific servers only | `discordBot.guildAllowlist` (comma-separated guild IDs) |
| Change how often ingestion runs | `ingestion.schedule` (cron; default `0 */6 * * *`) |
| Re-ingest every single commit | Enable webhooks: `webhook.enabled=true` + `secrets.webhookSecret` |
| Give the bot longer to wait for slow LLMs | `discordBot.askTimeoutSecs` (default 180) |
| Raise / lower token budgets | `rateLimits.guildTokensPerHour`, `rateLimits.userTokensPerHour` |
| Disable answer caching (for perf benchmarking) | `queryCache.enabled=false` |
| Turn off hybrid search (go vector-only) | `search.hybrid.enabled=false` |
| Plug in an Ollama reranker | `reranker.enabled=true` + `reranker.url` |
| Expose Prometheus metrics | `observability.serviceMonitor.enabled=true` |
| Switch to sealed-secrets | `secrets.existingSecret="<secret-name>"` |
| Scale rag horizontally | `ragOrchestrator.replicas` (bot must stay at 1 — Discord limit) |
| Clone a private repo | `secrets.gitToken` (GitHub fine-grained PAT) |
| Change container log verbosity | env `LOG_LEVEL` (default `INFO`; set in the Deployment/Job spec) |
| Change the embedding model | `models.embed` + `models.embedDim` — **warning: requires full re-ingest** |

## Repository being indexed

| Value | Default | Notes |
|---|---|---|
| `repo.name` | `example` | Slug; appears in `/ask` payloads and Postgres `chunks.repo`. Keep stable — changing means re-ingesting. |
| `repo.url` | `https://github.com/example/example.git` | HTTPS clone URL. Private repos need `secrets.gitToken`. |
| `repo.branch` | `main` | Any git ref. Re-ingestion follows this branch. |

## Discord bot

| Value | Default | Notes |
|---|---|---|
| `discordBot.replicas` | `1` | **Do not raise.** One Discord gateway per token is a hard API limit. |
| `discordBot.guildAllowlist` | `""` (allow all) | Comma-separated guild IDs. `/ask` replies "bot isn't enabled here" anywhere else. |
| `discordBot.askTimeoutSecs` | `180` | Bot gives up waiting for rag after this many seconds. Bump if you see `httpx.ReadTimeout` on cold-starts; drop to 60 if all your LLMs are hosted (always-warm). |
| `discordBot.resources.*` | `50m/128Mi` requests, `500m/256Mi` limits | Bot is idle most of the time — these are generous. |

**Runtime, not chart:** Message Content Intent must be toggled in the
Discord Developer Portal for thread follow-ups to work. See
`docs/discord-setup.md`.

## Models

| Value | Default | Notes |
|---|---|---|
| `models.embed` | `text-embedding-3-small` | Must match a LiteLLM model alias. **Changing invalidates every vector — requires full re-ingest.** |
| `models.embedDim` | `1536` | Must match the embedding model's output dim. |
| `models.chat` | *(removed)* | No chart default. The active chat model lives in Postgres (`instance_settings.chat_model`) and is set at runtime via `/model set <name>` in Discord. `/ask` replies "model not set" until an admin picks one. |

Runtime chat-model commands require Discord's **Manage Server** permission:

- `/model list` — show what LiteLLM exposes.
- `/model set <name>` — pick one. Validated against LiteLLM at write time.
- `/model current` — inspect the active model + who last changed it.

## LiteLLM

| Value | Default | Notes |
|---|---|---|
| `litellm.baseUrl` | `http://litellm.litellm.svc.cluster.local:4000` | Any OpenAI-compatible endpoint. Point at your homelab LiteLLM, a hosted service, or a mock. |

## Secrets

One sealed Secret (`secrets.existingSecret`) or inline plaintext —
**never commit the plaintext form** (`values-*.yaml` is gitignored for
this reason). See `deploy/SECRETS.md` for the `kubeseal` workflow.

| Key in the Secret | What it's for |
|---|---|
| `DISCORD_BOT_TOKEN` | Discord dev-portal Bot token |
| `LITELLM_API_KEY` | LiteLLM master/virtual key |
| `POSTGRES_DSN` | `postgresql://gitdoc_<slug>:<pw>@<host>:5432/gitdoc_<slug>?sslmode=require` |
| `GIT_TOKEN` | Optional; GitHub PAT for private repo clones. Leave empty for public. |
| `WEBHOOK_SECRET` | Optional; required when `webhook.enabled=true`. |

| Value | Default | Notes |
|---|---|---|
| `secrets.existingSecret` | `""` | Name of a pre-existing Secret (e.g. materialised by sealed-secrets). When set, the chart skips rendering its own plaintext Secret. |
| `secrets.discordBotToken` / `litellmApiKey` / `postgresDsn` / `gitToken` / `webhookSecret` | `""` | Bootstrap-only plaintext fields. Ignored when `existingSecret` is set. |

## Retrieval

| Value | Default | Notes |
|---|---|---|
| `search.hybrid.enabled` | `true` | Blend pgvector similarity with BM25 via RRF. Disable to fall back to vector-only if a backend issue looks fusion-related. |
| `reranker.enabled` | `false` | Cross-encoder reranker between retrieval and LLM. |
| `reranker.url` | `""` | Endpoint. Typically your homelab Ollama (e.g. `http://ollama.ollama.svc:11434/api/rerank`) or a small adapter. |
| `reranker.model` | `BAAI/bge-reranker-v2-m3` | Model name passed to the reranker. |
| `reranker.candidatesMultiplier` | `3` | Fetch `top_k * mult` candidates before rerank. Higher = better quality, slower. |
| `queryCache.enabled` | `true` | Short-circuit `/ask` on identical queries against the same commit SHA. Disable for benchmarking. |

Deep reference: `docs/retrieval.md`.

## Rate limits

| Value | Default | Notes |
|---|---|---|
| `rateLimits.enabled` | `true` | Gate `/ask` on rolling 1-hour token budgets. `guild_id` / `user_id` come from every Discord interaction. |
| `rateLimits.guildTokensPerHour` | `200000` | Soft ceiling per server. Tune based on typical conversation length × members × desired throughput. |
| `rateLimits.userTokensPerHour` | `50000` | Soft ceiling per user. Stops a single user from burning the guild's budget. |

Cache hits count as 0 tokens — free answers don't eat budget. Over-budget requests return 429 with a `retry_after` the bot renders as
"You're asking too fast. Retry in ~N seconds."

## Ingestion

| Value | Default | Notes |
|---|---|---|
| `ingestion.schedule` | `0 */6 * * *` | Cron (cluster timezone). Every 6h by default. Faster = fresher answers, more embedding cost. |
| `ingestion.embedBatch` | `64` | How many chunks per embedding-API call. |
| `ingestion.scratchSize` | `2Gi` | `emptyDir` sizeLimit for the clone workspace. Bump for very large repos. |
| `ingestion.resources.*` | `200m/512Mi` req, `2/2Gi` limit | CPU-bound during chunking; the limit drives wall time. |
| `dbMigrate.enabled` | `true` | Pre-install/upgrade Helm hook that applies `init.sql`. Don't disable unless you really know what you're doing. |
| `dbMigrate.image` | `postgres:16-alpine` | Must match the Postgres major version of your DB. |

### What ingestion indexes

Fixed in `services/ingestion/ingest.py`; not chart-configurable today.

- **Code:** `.py`, `.js`, `.jsx`, `.ts`, `.tsx`, `.go`, `.java`, `.rs`, `.rb`, `.php`, `.c`, `.cpp`, `.cs`, `.kt`, `.scala`, `.swift`
- **Docs:** `.md`, `.mdx`, `.rst`, `.txt`, `.adoc`
- **Skipped directories:** `.git`, `node_modules`, `dist`, `build`, `vendor`, `__pycache__`, `.venv`, `venv`, `target`, `out`
- **File size cap:** 500 KB

## Webhooks (optional)

| Value | Default | Notes |
|---|---|---|
| `webhook.enabled` | `false` | Exposes `POST /webhook` on the rag service. Renders a ServiceAccount + Role + RoleBinding so the pod can spawn Jobs from the CronJob. |
| `webhook.stalenessThresholdSecs` | `7200` | `/status/ingestion` reports "ok" within this window, "stale" beyond. Wire an alert to it. |

Full setup: `docs/webhooks.md`.

## Observability

| Value | Default | Notes |
|---|---|---|
| `observability.serviceMonitor.enabled` | `false` | Render a Prometheus Operator `ServiceMonitor`. Needs the CRD installed. |
| `observability.serviceMonitor.interval` | `30s` | Scrape interval. |
| `observability.serviceMonitor.scrapeTimeout` | `10s` | Per-scrape timeout. |
| `observability.serviceMonitor.labels` | `{}` | Extra labels (e.g. `release: kube-prometheus-stack` to match a selector). |

`/metrics` on the rag service is exposed regardless. Full PromQL snippets
+ metric names in `docs/observability.md`.

## Resource / scale

| Value | Default | Notes |
|---|---|---|
| `ragOrchestrator.replicas` | `2` | Safe to scale horizontally. Rate-limit buckets and per-repo caches are in-process, so two replicas may briefly disagree after a `/model set` (15s window). |
| `ragOrchestrator.resources.*` | `100m/256Mi` req, `1/1Gi` limit | The bulk of the work; bump if you're seeing OOMkills. |

## Images

| Value | Default | Notes |
|---|---|---|
| `images.{bot,rag,ingestion}.repository` | `ghcr.io/ralton-dev/gitdoc-*` | GHCR; public. Change if you fork. |
| `images.{bot,rag,ingestion}.tag` | matches chart `version` | Pin to `vX.Y.Z` in your per-instance values file for deterministic rollouts. |
| `images.{bot,rag,ingestion}.pullPolicy` | `IfNotPresent` | |

## Env-only (no chart value)

A few variables the service code reads but that aren't mapped to chart
values today. Override by patching the Deployment/Job env block
directly, or open a PR to add a chart value.

| Env var | Service | Default | Notes |
|---|---|---|---|
| `LOG_LEVEL` | all three | `INFO` | Stdlib logging level. JSON logs regardless. |
| `EMBED_MODEL` | rag, ingestion | `text-embedding-3-small` | Mirrors `models.embed`. |
| `EMBED_BATCH` | ingestion | `64` | Mirrors `ingestion.embedBatch`. |

## Changing chat models at runtime vs at deploy

The chat model is deliberately **not** a chart value. It lives in
Postgres so you can swap it without a redeploy. That also means it's
validated against the live LiteLLM `/v1/models` at `/model set` time —
no stale chart defaults routing traffic to a model the backend doesn't
actually expose.

Everything else that can change at runtime either (a) goes through
Discord slash commands or (b) lives in a sealed Secret the bot re-reads
on restart. No other Postgres-backed settings today.

## Related docs

- `README.md` — what this project is, what commands are available.
- `deploy/DEPLOY.md` — step-by-step first deploy.
- `docs/discord-setup.md` — Discord dev-portal walkthrough.
- `deploy/SECRETS.md` — sealed-secrets workflow.
- `docs/retrieval.md` — hybrid search, reranker, cache internals.
- `docs/webhooks.md` — webhook setup + signature verification.
- `docs/models.md` — runtime model-selection commands.
- `docs/observability.md` — metrics + structured logs.
- `docs/architecture.md` — how the services fit together (contributor-oriented).
