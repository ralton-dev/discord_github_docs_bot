# Retrieval

How the rag orchestrator picks the chunks it shows the chat model. Today
this covers **hybrid search** (plan 11) and the **cross-encoder reranker**
(plan 12). Future Wave 5 work — the query cache (plan 13) and request
rate-limiting (plan 14) — will land here as it ships.

## Hybrid search

Pure vector similarity is great for fuzzy intent ("how do I configure
X?") but mediocre for exact-match identifier lookups (`process_batch`,
`ENOENT`, `quokka_addition_marker_42`). Identifiers rarely embed well —
the embedding model has never seen most of them. Hybrid search blends
the two so each leg covers the other's weakness.

The orchestrator runs **two retrievals in parallel** for each `/ask`:

1. **Vector** — `ORDER BY embedding <=> $query_vec`, the existing
   pgvector cosine ANN.
2. **BM25** — `ORDER BY ts_rank_cd(content_tsv, plainto_tsquery(...))`
   over a Postgres full-text `tsvector` GIN index on `chunks.content`.

Both pull `top_k * 3` candidates so the fusion has signal beyond the
obvious top hits. The two rankings are merged with **Reciprocal Rank
Fusion** (RRF) and the top `top_k` survivors are returned.

### RRF, briefly

For each chunk in either ranking, score it as

```
rrf_score = sum_over_lists( 1 / (k + rank_in_list) )
```

with `k = 60` (the constant from the original RRF paper, Cormack et al.,
2009 — widely used as a safe default; lower `k` lets top ranks dominate,
higher `k` flattens their contribution). Sort by descending score, take
the top `top_k`. That's it.

The fusion is **rank-based, not score-based**, so it composes raw vector
similarities (in `[0, 2]`) with full-text rank scores (unbounded) without
needing to normalise either side.

The pure-Python implementation in `services/rag/app.py` is `_rrf_fuse`
and has its own unit tests in `services/rag/tests/test_hybrid.py`.

### Schema

The `chunks.content_tsv` column is a Postgres **stored generated column**:

```sql
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx
    ON chunks USING gin (content_tsv);
```

It stays in sync with `content` automatically on every INSERT/UPDATE — no
trigger, no application code. Ingestion is unchanged. The migration is
additive and idempotent (both the `ADD COLUMN` and the index use
`IF NOT EXISTS`).

The chart's pre-install/upgrade migration job (see
`deploy/helm/gitdoc/templates/db-migrate-job.yaml`) runs the canonical
`db/init.sql`, so `helm upgrade --install` picks the column up
automatically. Existing rows are backfilled by Postgres on the
`ALTER TABLE` itself; no separate backfill step is required.

## Disabling hybrid search

Hybrid is on by default. To fall back to vector-only — typically because
a backend issue looks fusion-related, or to A/B against the previous
behaviour — set:

```yaml
search:
  hybrid:
    enabled: false
```

…in your per-instance values file, then `helm upgrade --install`. The
chart wires this through to the `HYBRID_SEARCH_ENABLED` env var on the
rag pod; the dispatcher in `_retrieve` reads it at call time, not just
at import. (No restart trick yet — `helm upgrade` rolls the rag pods,
which is enough.)

The vector-only path stays in the codebase as `_retrieve_vector` so the
flip is genuinely a routing change, not a code path that can rot.

## Tuning

- **`RRF_K = 60`.** The standard. Don't change it without an evaluation
  set — the constant is the kind of knob that's easy to fiddle with and
  hard to verify.
- **Candidate width = `top_k * 3`.** Both legs fetch this many before
  fusion. Wider = more recall, more DB work; narrower = the fusion has
  less to work with. 3× is a defensive default; raise if a corpus shows
  the right answer landing past rank `top_k` on both legs but inside
  rank `top_k * 3` on at least one.
- **English text-search config.** `to_tsvector('english', ...)` and
  `plainto_tsquery('english', ...)` apply English stemming and
  stop-word removal. Identifiers like `process_batch` are split on the
  underscore by the english config — fine for typical queries, less
  fine if your corpus has a lot of `snake_case` exact-match needs. If
  this becomes a real problem, switch to the `simple` config (no
  stemming, no stop-words) on a per-instance basis; that's a one-line
  change in `_retrieve_hybrid`.

## Reranker

Hybrid search is recall-oriented — it pulls a generous candidate pool so
the right chunk is *somewhere* in the top N. A **cross-encoder reranker**
takes that pool and re-orders it by reading `(query, chunk)` jointly
rather than via independent embeddings. Cross-encoders are consistently
the highest-impact, lowest-cost RAG quality win: they slot between
retrieval and the LLM, filter the noisy hits a vector ANN inevitably
returns, and stop expensive LLM context being wasted on near-misses.

When the reranker is enabled, `/ask` does:

1. `_retrieve(repo, query, embedding, top_k * candidatesMultiplier)` —
   widen the initial pool. Default multiplier is 3, so a `top_k=6`
   request fetches 18 candidates.
2. POST those candidates + the query to the configured reranker URL.
3. Sort by descending relevance score, keep the top `top_k`, hand them
   to the LLM.

The relevant module is `services/rag/reranker.py`; the dispatch lives in
`/ask` between `_retrieve` and the chat call.

### Wiring up an endpoint

Pick whichever of these the operator can run:

- **Ollama** — pull the cross-encoder model and point `reranker.url` at
  the host. Some Ollama builds expose `/api/rerank` directly; others
  don't, in which case run a small adapter (a few lines of FastAPI in
  front of `sentence-transformers`) and point at that.
- **LiteLLM `/v1/rerank`** — already reachable from the cluster. The
  request/response shape differs from ours (LiteLLM returns
  `{"results": [{"index": int, "relevance_score": float}, ...]}`), so
  put a tiny adapter in front that re-marshals to our expected
  `{"scores": [float, ...]}` and point `reranker.url` at the adapter.

### Expected request / response shape

The reranker module speaks one shape only — keep adapters thin.

Request:

```json
POST <reranker.url>
Content-Type: application/json

{
  "model":     "BAAI/bge-reranker-v2-m3",
  "query":     "what does the calculator add function do",
  "documents": [
    {"text": "<chunk content 0>"},
    {"text": "<chunk content 1>"},
    {"text": "<chunk content 2>"}
  ]
}
```

Response (200):

```json
{ "scores": [0.13, 0.97, 0.42] }
```

Constraints: `scores` must be the **same length** as `documents` and in
the **same order** (index 0 = score for documents[0]). Higher score = more
relevant. Floats are coerced from any numeric type.

### Graceful degrade

The reranker is an enhancement; `/ask` MUST keep working when it is
unreachable, slow, or returning garbage. The module logs a warning and
returns the input candidates **unchanged** on every failure mode:

- Transport errors (connection refused, DNS, timeout, TLS, peer reset).
- HTTP `>= 400` responses.
- Non-JSON or otherwise unparseable bodies.
- Missing `scores` key, wrong-length `scores`, or non-numeric values.

Net effect: a wedged reranker quietly degrades to plain retrieval order.
Watch `gitdoc_rerank_latency_seconds` and the matching `rerank.*`
warnings in the logs to spot the degradation.

### Enabling

```yaml
# values-<slug>.yaml
reranker:
  enabled: true
  url: "http://ollama.ollama.svc.cluster.local:11434/api/rerank"
  model: "BAAI/bge-reranker-v2-m3"
  candidatesMultiplier: 3
```

Then `helm upgrade --install`. Env vars are read at import time, so a
`helm upgrade` (which rolls the rag pods) is the right way to flip the
flag — do not try to hot-swap it via a config edit.

### Tuning

- **`candidatesMultiplier = 3`.** Bumping to 4-5 buys recall at the cost
  of one extra LLM-irrelevant cross-encoder pass; the cross-encoder is
  ~O(n) so cost scales linearly with the multiplier. Past ~5 there's
  diminishing returns unless the underlying retrieval is poor (in which
  case fix retrieval, not the reranker).
- **Latency budget.** Target ~500ms p95 at `top_k=6` (so 18 candidates).
  The histogram buckets in `metrics.RERANK_LATENCY_SECONDS`
  (`0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0`) are picked so a healthy GPU
  reranker lives in the 0.1-0.5 buckets and a CPU-only fallback in the
  0.5-2 buckets. Anything in the 2-5 bucket is a slow path worth
  investigating; >5 is a timeout (default 5s) and falls through to
  un-reranked rows.
- **Model choice.** `BAAI/bge-reranker-v2-m3` is a sensible homelab
  default — multilingual, ~600MB, strong English performance. Smaller
  alternatives (`bge-reranker-base`) fit on CPU and stay under budget
  for `top_k=6`. Larger models (`Cohere/rerank-english-v3.0`) need a
  hosted endpoint and will dominate the latency budget.

### Disabling

Either:

```yaml
reranker:
  enabled: false
```

(then `helm upgrade --install`) — or leave `reranker.url` empty. Both
short-circuit the rerank step entirely; `/ask` runs as if the reranker
weren't installed.
