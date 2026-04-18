# Architecture

One bot instance per repo. Three stateless Python services talk to one
Postgres with `pgvector`. No inbound traffic required (unless `/webhook`
is enabled) — the Discord bot dials out.

## Component map

```
                      ┌──────────────────────┐
                      │    Discord gateway   │
                      │   (wss://…, TLS)     │
                      └──────────┬───────────┘
                                 │
              outbound websocket │ (bot dials out, Discord pushes events down)
                                 │
                  ┌──────────────▼──────────────┐
                  │    services/discord-bot     │
                  │ • /ask, /model, threads     │
                  │ • one gateway per token     │
                  │ • replicas: 1 (hard limit)  │
                  └──────────────┬──────────────┘
                                 │
                                 │  HTTP POST /ask, /models, /settings
                                 │  (ClusterIP, in-cluster only)
                                 │
                  ┌──────────────▼──────────────┐      ┌──────────────────┐
                  │       services/rag          │─────▶│     Prometheus   │
                  │ • FastAPI                   │ scrape /metrics
                  │ • hybrid retrieval          │
                  │ • reranker (optional)       │
                  │ • query cache               │
                  │ • per-user rate limits      │
                  │ • /webhook (optional)       │
                  └──┬──────────────────────┬──┘
                     │                      │
                     │ embed + chat         │ SQL (libpq, SSL)
                     ▼                      ▼
             ┌──────────────┐      ┌──────────────────┐
             │   LiteLLM    │      │     Postgres     │
             │ OpenAI-compat│      │ + pgvector + FTS │
             └──────────────┘      └────────▲─────────┘
                                            │
                                            │ INSERT chunks (cron + ad-hoc)
                                            │
                          ┌─────────────────┴─────────────────┐
                          │      services/ingestion           │
                          │ • CronJob (every 6h default)      │
                          │ • Jobs spawned by /webhook        │
                          │ • git clone + chunk + embed       │
                          └───────────────────────────────────┘
```

## How the Discord side links to the cluster side

The only thing that binds your Discord application to the running pod is
the **bot token**. There is no outbound configuration on Discord pointing
at your cluster.

1. You create a Discord application in the dev portal. Discord generates
   a token for it.
2. You put the token in a Kubernetes Secret (`gitdoc-<slug>`,
   `DISCORD_BOT_TOKEN` key).
3. The discord-bot pod starts, reads the token from its env, opens an
   outbound WebSocket to `gateway.discord.gg`, and presents the token.
4. Discord's gateway looks up the token, recognises it as your
   application, and marks the bot **online** in every server where it's
   been invited.

All subsequent interactions (a user typing `/ask`) flow *down* that same
already-open websocket — Discord pushes events, the pod responds on the
same connection. No ingress, no port forwarding, no dynamic DNS.

This means:

- Your homelab does not need to be reachable from the internet for the
  bot to work.
- Kicking the bot from a server doesn't invalidate the token — the app
  still exists.
- Rotating the token (Reset Token in the dev portal) immediately kills
  any running pod still using the old one.
- You can run the same token from only one place at a time. Running two
  pods with the same token fights for the gateway session.

## Request flow: /ask

```
1. Discord user types: /ask what does this project do

2. Discord gateway pushes an Interaction event DOWN the bot's
   existing websocket.

3. Bot pod:
   - Validates interaction.guild_id ∈ GUILD_ALLOWLIST (early reject
     if not, ephemeral reply).
   - Generates a uuid4 query_id for log correlation.
   - Calls orchestrator HTTP: POST rag:8000/ask
       body: {query, repo, top_k, guild_id, user_id}

4. Rag orchestrator:
   a. Rate-limit gate         → 429 if over guild/user hourly token budget
   b. Query-cache lookup      → return cached answer if hit
                                (keyed on repo, commit_sha, query_hash)
   c. Resolve chat model      → instance_settings.chat_model or env default
   d. Embed the query         → LiteLLM /v1/embeddings
   e. Retrieve candidates     → pgvector top_k*mult ∪ BM25 top_k*mult
                                fused via RRF (k=60)
   f. Rerank (if enabled)     → cross-encoder, keep top_k
   g. Chat completion         → LiteLLM /v1/chat/completions
                                with the retrieved chunks as context
   h. Record usage            → rate_limit_usage (tokens used)
   i. Write to query_cache    → if status=ok AND non-empty citations
   j. Emit metrics + logs     → queries_total, latencies, tokens,
                                ask.completed event

5. Bot pod:
   - Formats answer + sources list into one message (≤1990 chars).
   - Opens a public thread on the response and posts the answer there.
   - Thread auto-archives after 60 min of inactivity.

6. Future replies in that thread:
   - Bot detects via on_message + thread.parent ownership.
   - Collects last 10 messages, budgets at 3000 chars.
   - Compacts prior citations to `[src: path1, path2]` summaries.
   - Re-calls /ask with `history=[{role, content}, …]`.
   - On 422 (orchestrator doesn't support history) falls back to
     single-turn silently — one-time INFO log.
```

Cache hit path short-circuits at step 4b — returns in ms, no LLM call,
no token spend.

## Request flow: ingestion

```
Either:
  A. Scheduled CronJob fires (every 6h by default)
  B. POST /webhook with a valid signature spawns an ad-hoc Job from the
     same CronJob template (via in-cluster kubernetes client)

The Job pod:
1. git clone --depth=1 the target repo into an emptyDir
2. Walk files, skip vendored / binary / overlarge
3. Language-aware chunking (per extension, else generic)
4. Batched embeddings via LiteLLM (EMBED_BATCH=64 default)
5. Upsert chunks with pgvector embedding + tsvector content_tsv
6. GC old commits: DELETE FROM chunks WHERE commit_sha != current
7. GC stale cache: DELETE FROM query_cache WHERE commit_sha != current
8. UPDATE ingest_runs SET status='ok', chunk_count=…
9. Exit
```

The generated `content_tsv` column stays in sync with `content` for free
(`GENERATED ALWAYS AS … STORED`) — ingestion code doesn't touch it.

## Where things live

| Thing | Location |
|---|---|
| Durable: embeddings, metadata | Postgres `chunks`, `ingest_runs` |
| Durable: per-instance settings | Postgres `instance_settings` |
| Durable: answer cache | Postgres `query_cache` (pruned on new commit_sha) |
| Durable: rate-limit usage | Postgres `rate_limit_usage` (rolling 1h window) |
| Transient: clone workspace | Ingestion pod `emptyDir`, purged on exit |
| Transient: response cache | rag pod in-process (15s chat-model, 60s model list) |
| Ephemeral: in-flight queries | rag pod memory |

No PVCs. The only durable state is in Postgres, which you manage
separately (homelab cluster).

## Why each service exists separately

- **discord-bot** is thin, one replica, gateway websocket. Rare releases.
- **rag** is horizontally scalable (default `replicas: 2`), CPU-light,
  the hot path. Most iteration happens here.
- **ingestion** is a batch job. Running it in the same pod as rag would
  couple two very different lifecycles (long-running daemon vs short
  batch). Separate images mean separate Dockerfile bases — ingestion
  needs `git` installed; the others don't.

## Multi-instance

One Helm release = one repo = one namespace = one Discord bot token.
Scale out by deploying more releases; each gets its own Postgres role
and database (but all share the same Postgres cluster). The chart is
designed for this from day one — no shared cluster-scoped resources
between instances beyond the sealed-secrets controller.

## Where to extend

| Want to… | Touch |
|---|---|
| Change retrieval (re-rank, rerank model, hybrid weights) | `services/rag/app.py` + `docs/retrieval.md` |
| Add metrics / log events | `services/rag/metrics.py`, `logging_config.py` in each service |
| Add a new slash command | `services/discord-bot/bot.py` |
| New webhook provider | `services/rag/webhook.py` `verify_signature` |
| Postgres schema | `db/init.sql` (idempotent; applied by the db-migrate Helm hook) |
| Chart knobs | `deploy/helm/gitdoc/values.yaml` + templates |
| CI | `.github/workflows/{ci,release}.yml` |
