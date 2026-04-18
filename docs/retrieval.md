# Retrieval

How the rag orchestrator picks the chunks it shows the chat model. Today
this covers **hybrid search** (plan 11). Future Wave 5 work — the cross-
encoder reranker (plan 12), the query cache (plan 13), and request
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
