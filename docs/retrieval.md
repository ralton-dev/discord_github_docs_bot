# Retrieval

How the rag orchestrator picks the chunks it shows the chat model. Today
this covers **hybrid search** (plan 11), the **cross-encoder reranker**
(plan 12), the **query cache** (plan 13), and **rate limits** (plan 14).
Rate limiting sits at the top of `/ask` — before the cache — so a
rate-limited request doesn't consume a cache hit either.

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

## Query cache

In a busy Discord channel the same FAQ gets asked daily. Re-running
retrieval and the LLM for every repeat is pure waste. The **query
cache** short-circuits `/ask` when the same normalized question has
already been answered against the repo's current `commit_sha`, serving
the stored answer + citations straight out of Postgres without a single
embedding or chat call.

### What's keyed

The primary key is `(repo, commit_sha, query_hash)`:

- **`repo`** — the per-instance slug, same one the `chunks` table is
  keyed on.
- **`commit_sha`** — the most recent `status='ok'` row in
  `ingest_runs` for that repo, cached in-process for 15s.
- **`query_hash`** — SHA-256 of the query string after
  **normalisation**.

### Normalisation

The query is lowercased and its whitespace collapsed before hashing.
So `"What is X?"`, `"  what  IS  x?  "`, and `"WHAT IS X?"` all hit the
same cache row. Punctuation is preserved — two questions that differ
only in phrasing land on the same row, but semantically different ones
(different punctuation, different words, different order) must not.

### Invalidation

Eviction is driven by the ingestion path, not by a wall-clock TTL. When
a new ingestion run completes successfully with a different
`commit_sha`, the ingest job runs:

```sql
DELETE FROM query_cache WHERE repo = %s AND commit_sha <> %s;
```

So every cached answer for the prior commit is dropped in the same
transaction that GC'd the prior chunks. The `commit_sha` key means a
stale answer can't accidentally leak across ingestions even if the
delete raced — a read against the old SHA would simply return a miss
because the in-process 15s cache on `_latest_commit_sha` has already
rolled over.

### Metrics

Two counters on the default Prometheus registry, both labelled by
`repo`:

- `gitdoc_cache_hits_total{repo=…}`
- `gitdoc_cache_misses_total{repo=…}`

**PromQL for cache hit rate over the last 5 minutes**:

```promql
sum by (repo) (rate(gitdoc_cache_hits_total[5m]))
/
(
  sum by (repo) (rate(gitdoc_cache_hits_total[5m]))
  + sum by (repo) (rate(gitdoc_cache_misses_total[5m]))
)
```

A healthy FAQ-heavy channel sits in the 0.3-0.7 range. A hit rate
near 0 means either nobody is re-asking (fine), ingestion is too
frequent (invalidation is churning the cache faster than users hit
repeats), or the cache is disabled. Near 1.0 = basically every query
has been asked before (still fine — this is pure latency win).

Cache hits appear in `gitdoc_queries_total` under
`status="cache_hit"` — separate from the `ok` bucket so dashboards can
distinguish cached from fresh answers at a glance.

### Disabling

On by default. Flip off per-instance with:

```yaml
queryCache:
  enabled: false
```

…then `helm upgrade --install`. The env var `QUERY_CACHE_ENABLED` is
read at import time, so the flag-flip takes effect on the next pod
roll (which `helm upgrade` triggers automatically). Use this when:

- Benchmarking the cold path (otherwise the repeat runs show up as
  spuriously fast).
- Isolating a suspected stale-answer bug — disable, upgrade, then
  compare responses against the cached-on version.
- Running a model change where the operator wants every answer
  re-generated under the new chat model without waiting for ingestion
  to evict.

### Operational notes

- Cache DB calls are wrapped in try/except — a flaky Postgres degrades
  to "every request misses" rather than failing `/ask`. Watch for
  `query_cache lookup failed` / `query_cache insert failed` log
  events if the hit rate collapses unexpectedly.
- Write-through uses `ON CONFLICT (repo, commit_sha, query_hash) DO
  NOTHING`, so a race between concurrent slow-path requests on the
  same question is safe — both answers are valid for the same
  commit_sha, the first writer wins.
- Empty-citation answers (the "I couldn't find anything relevant"
  path) and any error response are **never** cached — we don't want
  to pin a no-op response that ingestion didn't cause.
- The `hits` column bumps on every cache hit. Handy for "what are the
  most asked questions?" analytics:
  ```sql
  SELECT repo, left(answer, 80) AS answer_preview, hits, created_at
  FROM query_cache
  WHERE hits > 0
  ORDER BY hits DESC
  LIMIT 20;
  ```

## Rate limits & cost caps

`GUILD_ALLOWLIST` is a blunt on/off gate — a guild is either allowed to
talk to the bot or it isn't. Rate limiting is the **budget** on top of
that: even an allowed guild can't burn through 10k queries overnight.
Two independent token caps are enforced on every `/ask` that carries a
Discord identity:

- **Per-guild**: `GUILD_TOKENS_PER_HOUR` — total tokens across all
  users in a single guild over a rolling 1-hour window. Default 200k.
- **Per-user**: `USER_TOKENS_PER_HOUR` — total tokens from one user
  across every guild they're in. Default 50k.

When either cap is exceeded the orchestrator returns HTTP 429 with:

```json
{ "error": "guild_budget" | "user_budget", "retry_after": <secs> }
```

`retry_after` is the number of seconds until the oldest in-window row
ages out — the earliest point at which the bucket can plausibly drain
below the cap. The Discord bot catches 429 specifically (separately
from "backend down") and posts an ephemeral friendly message:

```
You're asking too fast. Retry in ~<N> seconds.
(Budget: <reason>)
```

Caps live in `.Values.rateLimits` so operators can tune per-instance
without touching code.

### Precedence: guild first, then user

Guild cap is checked before user cap. Whichever trips first wins — the
caller gets exactly one `reason` label. If both buckets are over on the
same call, we report `guild_budget`. That's deliberate: a guild-wide
problem is more actionable ("who's the heaviest user in this guild?")
than a per-user one.

### What counts as "tokens"

On the happy path, `prompt_tokens + completion_tokens` from the
OpenAI-compatible chat response. Ollama routes through LiteLLM often
return `0/0` for usage — in that case the call is accounted as 0
tokens, which underreports actual spend. If a homelab instance really
runs exclusively on Ollama the rate limits become an effective
request-count cap via the row-count per (guild, user) not the token
sum; raise the caps accordingly or add a request-count cap if this
matters.

**Cache hits are free.** A `status="cache_hit"` /ask records one
`rate_limit_usage` row with `tokens=0` — the row exists so request-
count analytics stay coherent, but the budget math ignores it.

**Empty-retrieval answers** (`"I couldn't find anything relevant..."`)
also don't charge a token budget — the chat call never ran.

### Non-Discord callers bypass the gate

When both `guild_id` and `user_id` are absent from the /ask body the
gate is skipped entirely. Used by:

- curl probes from inside the cluster (debugging, smoke tests).
- health-check flows that hit /ask as a deep readiness check.

This is intentional: rate limiting protects against runaway *users*, not
against the operator. If an external tool should be rate-limited, add
synthetic `guild_id="ext-<tool>"` + `user_id="ext-<tool>"` to its
requests — the cap applies to any identity tuple, Discord or not.

### Metrics

One counter on the default Prometheus registry:

- `gitdoc_rate_limit_hits_total{repo=..., reason=...}`
  - `reason`: `guild_budget` | `user_budget`

429'd requests also show up in `gitdoc_queries_total` with
`status="rate_limited"` so dashboards can distinguish "rejected for
budget" from "failed for technical reason".

**PromQL for "which guild is hitting the cap?"**:

```promql
sum by (repo) (
  increase(gitdoc_rate_limit_hits_total{reason="guild_budget"}[1h])
)
```

A steady-state >0 on any repo means either the cap is too tight for
legitimate use (bump it) or a single user is exhausting the guild
budget (look at `reason="user_budget"` on the same repo — if it's
also >0, same user is probably responsible for both).

### Tuning

- **`guildTokensPerHour = 200000`** -> ballpark 100-200 generous queries
  per hour depending on answer length. FAQ-heavy channels should
  comfortably stay below this; high-volume RAG usage may need 500k.
- **`userTokensPerHour = 50000`** -> ballpark 25-50 generous queries per
  hour per user. Low enough that one enthusiastic user can't burn the
  guild cap alone.
- Set either to an absurdly high number (e.g. `10000000`) to
  effectively disable that bucket without disabling the feature as a
  whole.
- Set `rateLimits.enabled: false` to disable accounting + enforcement
  entirely — keeps the DB table around for inspection but never writes
  to it.

### Invalidation / cleanup

Rows age out naturally: the SUM query filters on
`window_at > now() - interval '1 hour'`, so anything older is ignored
even if it's still in the table. No TTL or eviction job is required at
homelab scale. If the table grows inconveniently large, a daily cron
`DELETE FROM rate_limit_usage WHERE window_at < now() - interval '1 day'`
is enough — keep 24h for diagnostics, drop the rest.

### Graceful degrade

Every DB touch in the rate-limit path is wrapped in try/except:

- Check failure -> "allowed". /ask proceeds. A wedged Postgres should
  not 429 the user because of a rate-limit bug.
- Record failure -> swallowed. The answer is already generated; missing
  one usage row is preferable to 500-ing after a billable call.

Watch for `rate_limit_check failed` / `rate_limit_usage insert failed`
log events if you see the caps behaving weirdly.

### Disabling

Set `rateLimits.enabled: false` in the per-instance values file, then
`helm upgrade --install`. Env vars are read at import time so the pod
roll is the flag-flip. Kill-switch pattern:

```yaml
# values-<slug>.yaml
rateLimits:
  enabled: false
```
