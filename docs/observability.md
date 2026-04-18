# Observability

All three gitdoc services emit **structured JSON logs** to stdout, and the
rag orchestrator additionally exposes **Prometheus metrics** on `/metrics`.
This doc covers both.

## Metrics

The rag service runs `prometheus-client` and exposes the standard Prometheus
text format at `GET /metrics` on port 8000. No auth — the endpoint is
scraped in-cluster from a Prometheus pod that can reach the rag Service.

### Defined metrics

| Name | Type | Labels | What it tracks |
|---|---|---|---|
| `gitdoc_queries_total` | Counter | `repo`, `status` | One increment per `/ask`. `status` is `ok` (answer returned), `empty` (no retrieval hits), or `error` (502/5xx). |
| `gitdoc_tokens_prompt_total` | Counter | `repo`, `model` | Cumulative prompt tokens sent to the chat backend. Source: `completion.usage.prompt_tokens`. Silently omitted when the backend doesn't return usage (some LiteLLM routes / Ollama). |
| `gitdoc_tokens_completion_total` | Counter | `repo`, `model` | Cumulative completion tokens returned by the chat backend. Same source as above. |
| `gitdoc_retrieval_hits` | Histogram | `repo` | Number of context rows pgvector returned per `/ask`. Buckets: `[0, 1, 3, 5, 10, 20]`. The `0` bucket is the alert signal for "no relevant content indexed". |
| `gitdoc_latency_seconds` | Histogram | `endpoint` | End-to-end handler latency. Today `endpoint="ask"`. Buckets: `[0.1, 0.5, 1, 2, 5, 10, 30]`. |
| `gitdoc_embed_latency_seconds` | Histogram | `repo` | Latency of the embedding backend call during `/ask`. Default `prometheus_client` buckets. |
| `gitdoc_chat_latency_seconds` | Histogram | `repo`, `model` | Latency of the chat-completion backend call during `/ask`. Default buckets. |

### Suggested PromQL alerts

The four alerts named in the task brief:

- **Token burn** (spend > threshold in the last hour, per repo):
  ```promql
  sum by (repo) (rate(gitdoc_tokens_completion_total[1h])) > 10000
  ```
  Threshold is tokens/sec; scale to your budget (e.g. 10k tokens/sec ≈ 36M/h,
  roughly $5-10/h depending on model).

- **p99 latency > 10s** (end-to-end ask latency):
  ```promql
  histogram_quantile(
    0.99,
    sum by (le) (rate(gitdoc_latency_seconds_bucket{endpoint="ask"}[5m]))
  ) > 10
  ```

- **Retrieval-miss ratio > 10%** (empty retrievals as a share of all asks):
  ```promql
  (
    sum by (repo) (increase(gitdoc_retrieval_hits_bucket{le="0"}[10m]))
    /
    sum by (repo) (increase(gitdoc_retrieval_hits_count[10m]))
  ) > 0.10
  ```
  Fires when either ingestion is stale or the question corpus drifted away
  from what was indexed — both warrant a re-ingest review.

- **Ingestion failure rate > 0** (any failed run, per repo):
  ```promql
  # From the ingest_runs table via a sidecar exporter, OR read the status
  # from the rag service's `GET /status/ingestion?repo=<slug>`. We do not
  # currently emit a dedicated metric here — leave this as a black-box
  # check until a real pain point justifies an `ingestion_runs_total{status}`
  # counter.
  ```

### Enabling the ServiceMonitor

The chart ships a gated ServiceMonitor for the Prometheus Operator. Default
is **disabled** so clusters without the operator CRDs don't fail to apply.

In `values-<slug>.yaml`:
```yaml
observability:
  serviceMonitor:
    enabled: true
    interval: 30s
    scrapeTimeout: 10s
    # Match whatever label your Prometheus instance's
    # `serviceMonitorSelector` requires.
    labels:
      release: kube-prometheus-stack
```

Then `make helm-install REPO=<slug>`. Verify:
```sh
kubectl -n gitdoc-<slug> get servicemonitor
kubectl -n gitdoc-<slug> port-forward svc/gitdoc-<slug>-rag 8000:8000
curl -s http://localhost:8000/metrics | head
```

If the ServiceMonitor exists but Prometheus isn't scraping, 99% of the time
the fix is the `labels:` map — your operator selects on specific labels.
Check:
```sh
kubectl -n <prom-ns> get prometheus -o yaml | grep -A3 serviceMonitorSelector
```

## Structured JSON logs

All three services emit one JSON object per line on stdout, shaped like:
```json
{"ts":"2026-04-18T12:00:00.123Z","level":"INFO","logger":"gitdoc.rag","msg":"ask completed","event":"ask.completed","repo":"example","status":"ok","latency_ms":742,"hits":6,"prompt_tokens":1942,"completion_tokens":187,"model":"ollama_chat/llama3.2:3b"}
```

Required keys: `ts`, `level`, `logger`, `msg`. Anything passed as
`logger.info("...", extra={...})` is merged in at the top level. Exceptions
appear under `exc`.

### Events emitted

**rag (`gitdoc.rag`)**:
- `ask.received` — at handler entry. Fields: `repo`, `query_chars`.
- `ask.completed` — at handler exit (ok / empty / error alike). Fields:
  `repo`, `status`, `latency_ms`, `hits`, `prompt_tokens`,
  `completion_tokens`, `model`.

**ingestion (`gitdoc.ingest`)**:
- `ingest.start` — Fields: `repo`, `url`, `branch`.
- `ingest.cloned` — Fields: `repo`, `sha`.
- `ingest.batch` — one per flushed batch. Fields: `repo`, `batch_size`, `total`.
- `ingest.gc` — after old-commit GC. Fields: `repo`, `deleted`.
- `ingest.complete` — Fields: `repo`, `sha`, `chunks`, `deleted`, `duration_ms`.
- `ingest.failed` — Fields: `repo`, `sha`, `duration_ms`. Includes `exc` traceback.

**bot (`gitdoc.bot`)**:
- `bot.ask` — on `/ask` slash command. Fields: `query_id` (uuid4), `guild_id`, `repo`, `single`.
- `bot.thread_followup` — on a follow-up message in a bot-owned thread. Fields: `query_id`, `guild_id`, `repo`.
- `bot.response` — after replying to the user. Fields: `query_id`, `guild_id`, `repo`, `response_chars`, `outcome` (`ok` / `empty` / `error`).

### Reading the logs

`kubectl logs` + `jq`:

```sh
# All errors across the rag pods:
kubectl -n gitdoc-<slug> logs deploy/gitdoc-<slug>-rag --tail=1000 \
  | jq -c 'select(.level == "ERROR")'

# Filter to one event type:
kubectl -n gitdoc-<slug> logs deploy/gitdoc-<slug>-rag --tail=1000 \
  | jq -c 'select(.event == "ask.completed")'

# Filter by repo (when one orchestrator fronts multiple repos):
kubectl logs deploy/gitdoc-<slug>-rag --tail=1000 \
  | jq -c 'select(.repo == "example")'

# Trace a single query end-to-end from the bot through the rag response:
# 1. Find the query_id for the user's question:
kubectl -n gitdoc-<slug> logs deploy/gitdoc-<slug>-bot --tail=1000 \
  | jq -c 'select(.event == "bot.ask" and .guild_id == 123456789012345678)' \
  | jq -r '.query_id' | tail -1
# 2. Pull every log line with that query_id across both services:
Q=<query_id-from-step-1>
kubectl logs deploy/gitdoc-<slug>-bot --tail=2000 \
  | jq -c "select(.query_id == \"$Q\")"

# Ingestion throughput over the last run:
kubectl -n gitdoc-<slug> logs -l app.kubernetes.io/component=ingestion --tail=500 \
  | jq -c 'select(.event | startswith("ingest."))'
```

### Control

- Level: set `LOG_LEVEL` on the pod (`INFO` by default). `DEBUG` enables
  verbose discord.py / fastapi debug output — noisy, use for acute debugging.
- Formatter: the JSON formatter is installed at module import time in each
  service (`logging_config.configure()`). Calling it twice is idempotent —
  handlers are reused, not stacked.
