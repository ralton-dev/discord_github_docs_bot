"""End-to-end integration tests for gitdoc.

Each test brings up:

- A pgvector/pgvector:pg16 container with `db/init.sql` applied (session-scoped).
- An in-process FastAPI mock implementing `/v1/embeddings` and
  `/v1/chat/completions` (session-scoped).

…then exercises the real `services/ingestion/ingest.py` and
`services/rag/app.py` against them. No service code is refactored
to support the tests; they all rely on env-var injection that the production
modules already read at import time.

Marker `integration` lets the suite be selected with `pytest -m integration`
and excluded by the default unit-test run.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Ingestion path.
# ---------------------------------------------------------------------------

def test_ingest_populates_db(clean_db: str, ingest_module) -> None:
    """`ingest.main()` should populate `chunks` and mark `ingest_runs` ok."""
    ingest_module.main()

    with psycopg.connect(clean_db, autocommit=True) as conn:
        chunk_rows = conn.execute(
            "SELECT path, content_type FROM chunks WHERE repo = %s",
            ("sentinel",),
        ).fetchall()
        run_rows = conn.execute(
            "SELECT status, chunk_count FROM ingest_runs WHERE repo = %s",
            ("sentinel",),
        ).fetchall()

    assert chunk_rows, "expected at least one chunk for fixture repo"
    paths = {p for p, _ in chunk_rows}
    # All four fixture files (README.md, src/calculator.py, src/eventbus.py,
    # docs/architecture.md, docs/glossary.md) should be represented.
    expected = {
        "README.md",
        "src/calculator.py",
        "src/eventbus.py",
        "docs/architecture.md",
        "docs/glossary.md",
    }
    missing = expected - paths
    assert not missing, f"missing paths in chunks table: {missing}"

    assert run_rows, "expected one ingest_runs row"
    assert len(run_rows) == 1
    status, chunk_count = run_rows[0]
    assert status == "ok"
    assert chunk_count == len(chunk_rows)


# ---------------------------------------------------------------------------
# Query path.
# ---------------------------------------------------------------------------

def _seed(clean_db_dsn: str, ingest_module) -> None:
    """Helper: run ingestion once so the query tests have data to read."""
    ingest_module.main()


def test_query_returns_citation_pointing_at_source(
    clean_db: str, ingest_module, rag_app
) -> None:
    """A query whose answer lives in exactly one file should cite that file."""
    _seed(clean_db, ingest_module)

    client = TestClient(rag_app.app)
    # The phrase "database pgvector similarity search" only appears in
    # docs/architecture.md (the fixture is constructed so each fact has
    # exactly one source).
    resp = client.post("/ask", json={
        "query": "what does the database use for similarity search",
        "repo": "sentinel",
        "top_k": 3,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["citations"], "expected at least one citation"
    paths = [c["path"] for c in body["citations"]]
    assert "docs/architecture.md" in paths, (
        f"architecture.md should be cited; got {paths}"
    )
    # Top-1 should be the unique source file.
    assert paths[0] == "docs/architecture.md", (
        f"expected docs/architecture.md ranked top-1; got {paths}"
    )

    # The mock chat endpoint echoes the prompt prefix; assert we got *some*
    # answer string back so we know the chat call succeeded.
    assert isinstance(body["answer"], str) and body["answer"]


def test_query_for_add_function_cites_calculator(
    clean_db: str, ingest_module, rag_app
) -> None:
    """Second source-file probe — different fact, different file."""
    _seed(clean_db, ingest_module)

    client = TestClient(rag_app.app)
    resp = client.post("/ask", json={
        "query": "what does the add function compute return sum integers",
        "repo": "sentinel",
        "top_k": 3,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()

    paths = [c["path"] for c in body["citations"]]
    assert "src/calculator.py" in paths, (
        f"calculator.py should be cited for the add function; got {paths}"
    )


def test_query_with_no_matches_returns_empty_citations(
    clean_db: str, ingest_module, rag_app
) -> None:
    """An unknown repo (no rows) should produce the canned 'no matches' answer."""
    _seed(clean_db, ingest_module)

    client = TestClient(rag_app.app)
    resp = client.post("/ask", json={
        "query": "anything at all",
        "repo": "nonexistent-repo-name",  # nothing was ingested under this name
        "top_k": 3,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["citations"] == []
    assert "couldn't find anything" in body["answer"].lower()
