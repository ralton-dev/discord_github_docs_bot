---
status: todo
phase: 3
priority: low
---

# 13 · Query cache

## Goal
Cache completed answers keyed by `(repo, commit_sha, query_hash)` in Postgres with a short TTL, returning cached answers without re-running retrieval or the LLM.

## Why
In a busy Discord channel, the same FAQ gets asked daily. Answering from cache is free, instant, and still grounded to the commit that produced it.

## Acceptance criteria
- [ ] New `query_cache` table with `(repo, commit_sha, query_hash) PRIMARY KEY`, `answer`, `citations JSONB`, `created_at`, `hits` counter.
- [ ] Orchestrator looks up the cache before retrieval; on hit, increments `hits` and returns immediately.
- [ ] Cache entries invalidated when a new `commit_sha` appears for that repo (ingestion task deletes stale rows).
- [ ] A Prometheus counter exposes cache hit/miss rates.
- [ ] Integration test verifies identical queries hit the cache on the second call.

## Implementation notes
- Hash queries with SHA-256 of the normalized (lowercased, trimmed) query string.
- Don't cache empty/error results — only successful completions.
- TTL can be "until next ingestion" rather than wall-clock; the `commit_sha` key handles this naturally.

## Dependencies
- 04 (baseline working)
- 09 (metrics exist to track hit rate)
