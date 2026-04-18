---
status: todo
phase: 2
priority: medium
---

# 07 · Integration tests

## Goal
An end-to-end test that boots the compose stack, ingests a small fixture repo, issues a query, and asserts the orchestrator returns citations pointing at the right files.

## Why
Unit tests don't catch schema/index mismatches, embedding dimension drift, or LiteLLM misconfiguration. An e2e test does, and keeps the ingestion→retrieval→LLM contract intact as we evolve it.

## Acceptance criteria
- [ ] A tiny fixture repo (< 20 files) lives under `tests/fixtures/repo/` and contains deliberate "facts" the test can probe for.
- [ ] Test suite can run against the compose stack from task 01.
- [ ] LLM call can be stubbed (e.g., via a LiteLLM fake or a recorded response) so CI doesn't require a real API key.
- [ ] At least one test asserts the cited file matches the fact's source file.
- [ ] Test suite takes less than 2 minutes.

## Implementation notes
- Prefer `testcontainers-python` over raw compose for CI — tests that own their containers are less flaky.
- The reranker (task 12), hybrid search (task 11), and cache (task 13) should all add their own integration test cases as they land.
- For the stubbed LLM, LiteLLM has a `mock_response` feature in its config — use that instead of building a separate mock server.

## Dependencies
- 01 (compose stack)
- 06 (pytest setup)
