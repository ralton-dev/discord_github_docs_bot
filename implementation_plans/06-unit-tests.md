---
status: todo
phase: 2
priority: medium
---

# 06 · Unit tests

## Goal
Add a `pytest` test suite covering the pure-logic parts of each service: chunking rules, citation formatting, prompt construction, and any URL-munging helpers.

## Why
LLM outputs are non-deterministic, so integration tests alone are flaky as a signal. Unit tests pin down the deterministic glue — chunk sizes, skipped file types, citation truncation — where bugs hide.

## Acceptance criteria
- [ ] `pytest` runnable from the repo root with a single command; no real network required.
- [ ] Coverage for at least: `ingest.iter_chunks` (skip rules, language detection), bot's `_format` (truncation + no-citation case), `_auth_url` (token interpolation + no-token passthrough).
- [ ] CI (task 08) runs the suite on every PR.
- [ ] Any dependency on external services is injected (so tests can substitute fakes).

## Implementation notes
- `pytest` + `pytest-cov` is the baseline; no framework beyond that for this scale.
- Keep tests colocated with their service (`services/<svc>/tests/`) so each Dockerfile can optionally run them during build.
- The RAG orchestrator's `_retrieve` needs a Postgres connection — either mock it, or defer retrieval testing to the integration suite (task 07).

## Dependencies
None.
