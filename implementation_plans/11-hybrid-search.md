---
status: todo
phase: 3
priority: medium
---

# 11 · Hybrid search

## Goal
Blend keyword (BM25 / `tsvector`) search with vector similarity so queries that mention specific identifiers (function names, error strings) retrieve the right chunks, not just semantically similar ones.

## Why
Pure vector search is great for fuzzy intent ("how do I configure X?") but mediocre for exact-match lookups ("what does `process_batch` do?"). Identifiers rarely embed well. Hybrid search is table-stakes for a code-aware bot.

## Acceptance criteria
- [ ] `chunks` table grows a `content_tsv tsvector` column with a GIN index, populated on insert via trigger or by the ingestion service.
- [ ] Retrieval uses Reciprocal Rank Fusion (RRF) to merge vector and BM25 rankings.
- [ ] Integration test (task 07) includes a query that only BM25 can answer (exact identifier) and asserts it succeeds.
- [ ] A value flag lets operators disable hybrid and fall back to vector-only.

## Implementation notes
- Schema migration: additive. Drop into a new `db/init.sql` section or a numbered migration file.
- RRF constant `k=60` is the standard; tune if recall looks poor.
- Postgres full-text is good enough; don't introduce Elasticsearch.

## Dependencies
- 04 (baseline working)
