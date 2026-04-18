# Integration tests

End-to-end tests that exercise the real `services/ingestion/ingest.py` and
`services/rag/app.py` against:

- a `pgvector/pgvector:pg16` Postgres container booted via `testcontainers`
  with `db/init.sql` applied at startup, and
- a tiny in-process FastAPI mock implementing the OpenAI-compatible
  `/v1/embeddings` and `/v1/chat/completions` endpoints (deterministic output
  so cosine ranking is stable).

A small fixture repo lives at `tests/fixtures/repo/` with deliberately
unique facts; each test asserts the orchestrator's citations point at the
exact source file for the fact in question.

## Pre-requisites

- A running Docker daemon (Docker Desktop, OrbStack, colima — anything
  testcontainers can talk to).
- The dev requirements installed:

  ```
  pip install -r requirements-dev.txt \
              -r services/ingestion/requirements.txt \
              -r services/rag/requirements.txt \
              -r services/discord-bot/requirements.txt
  ```

No real OpenAI / Anthropic / LiteLLM API key is needed — the mock owns that
contract.

## Running

```bash
make integration-test
# or
pytest -m integration tests/integration
```

The default `make test` (and bare `pytest`) skips this suite via the
`addopts = -m "not integration"` directive in `pytest.ini`.

## Expected runtime

~30–60 seconds on a warm Docker daemon (the pgvector image must be cached
locally; first run pulls ~150 MB and adds another ~30 s). Total wall-time
budget: under 2 minutes.

## What the tests cover

- `test_ingest_populates_db` — `ingest.main()` writes chunks for every
  fixture file and marks `ingest_runs.status = 'ok'`.
- `test_query_returns_citation_pointing_at_source` — `/ask` with a query
  that uniquely matches `docs/architecture.md` returns that file as the
  top-1 citation.
- `test_query_for_add_function_cites_calculator` — second source-file
  probe targeting `src/calculator.py`.
- `test_query_with_no_matches_returns_empty_citations` — querying a repo
  name that was never ingested returns the canned "I couldn't find
  anything" answer with `citations == []`.
