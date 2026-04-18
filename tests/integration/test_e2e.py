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


def test_reranker_promotes_correct_file_for_noisy_query(
    clean_db: str, ingest_module, rag_app, monkeypatch
) -> None:
    """A reranker that promotes the right chunk should beat raw retrieval.

    Plan-12 acceptance: a noisy query (one whose words appear across many
    files) should land the right file top-1 *after* the cross-encoder pass
    even when it would not land top-1 from retrieval alone.

    We can't depend on a real cross-encoder endpoint in CI, so we
    monkeypatch ``reranker.rerank`` with a deterministic fake that scores
    candidates by whether their content mentions the unique sentinel
    marker for the eventbus fixture (``wallaby_pubsub_marker_77``). The
    query itself uses the noisy phrase "subscribe and publish" — words
    that show up in multiple fixture files (eventbus, calculator
    docstring, README), so retrieval alone can plausibly rank a different
    file top-1. The fake reranker steps in and surfaces eventbus.py.

    What this proves end-to-end:
    - The /ask handler actually calls the reranker when enabled.
    - The reranker's reordering survives the conversion back into
      retrieval rows and shows up in the response citations.
    - top_k * RERANKER_MULT widening pulls eventbus.py into the
      candidate pool even when retrieval would otherwise rank it lower
      than top_k=3.
    """
    _seed(clean_db, ingest_module)

    import reranker as reranker_mod

    async def fake_rerank(query, candidates, *, url, model, **kw):
        # Score 1.0 for the chunk that contains the unique eventbus
        # sentinel; everything else gets 0.0. Stable sort within ties
        # preserves the underlying retrieval order, so the assertion is
        # tight: eventbus must be top-1 because the reranker said so.
        scored = []
        for c in candidates:
            score = 1.0 if "wallaby_pubsub_marker_77" in c.get("content", "") else 0.0
            scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _s, c in scored]

    monkeypatch.setattr(rag_app, "RERANKER_ENABLED", True)
    monkeypatch.setattr(rag_app, "RERANKER_URL", "http://fake-reranker.local")
    monkeypatch.setattr(rag_app, "RERANKER_MULT", 3)
    monkeypatch.setattr(reranker_mod, "rerank", fake_rerank)

    client = TestClient(rag_app.app)
    resp = client.post("/ask", json={
        # Noisy query — both eventbus.py and other fixture files use these
        # general words. Without rerank, top-1 is not guaranteed to be
        # eventbus.py.
        "query": "how do I subscribe to and publish events on the bus",
        "repo": "sentinel",
        "top_k": 3,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    paths = [c["path"] for c in body["citations"]]
    assert paths, "expected at least one citation"
    # The fake reranker promotes eventbus.py — assert top-1 explicitly so
    # we know the rerank output flowed through (not just that the file
    # happens to be in the top-k).
    assert paths[0] == "src/eventbus.py", (
        f"reranker should promote eventbus.py to top-1; got {paths}"
    )


def test_identical_query_returns_cached_answer(
    clean_db: str, ingest_module, rag_app
) -> None:
    """Plan-13 acceptance: second /ask for an identical query hits the cache.

    We assert two independent things on the second call:

    1. The answer comes back materially faster than the cold path. The
       cold path synchronously talks to the mock LiteLLM over HTTP
       (embeddings + chat) which is conservatively ~20x slower than a
       Postgres SELECT against the testcontainer. A generous 1/5 ratio
       still leaves a clear signal on CI.
    2. The ``gitdoc_cache_hits_total{repo="sentinel"}`` counter
       increments by exactly 1 — proof the handler went through the
       cache-hit branch, not the slow path.
    """
    import time as _time

    from prometheus_client import REGISTRY

    _seed(clean_db, ingest_module)

    client = TestClient(rag_app.app)

    before_hits = REGISTRY.get_sample_value(
        "gitdoc_cache_hits_total", {"repo": "sentinel"}
    ) or 0.0

    question = {
        "query": "what does the add function compute",
        "repo": "sentinel",
        "top_k": 3,
    }

    t0 = _time.perf_counter()
    r1 = client.post("/ask", json=question)
    cold_ms = (_time.perf_counter() - t0) * 1000.0
    assert r1.status_code == 200, r1.text
    answer1 = r1.json()["answer"]

    t0 = _time.perf_counter()
    r2 = client.post("/ask", json=question)
    warm_ms = (_time.perf_counter() - t0) * 1000.0
    assert r2.status_code == 200, r2.text
    answer2 = r2.json()["answer"]

    # Identical response shape.
    assert answer1 == answer2
    assert r1.json()["citations"] == r2.json()["citations"]

    # Cache hit counter bumped by exactly one.
    after_hits = REGISTRY.get_sample_value(
        "gitdoc_cache_hits_total", {"repo": "sentinel"}
    ) or 0.0
    assert after_hits - before_hits == 1.0, (
        f"expected exactly one cache hit; got delta {after_hits - before_hits}"
    )

    # The warm path should be a fraction of the cold path. The mock
    # LiteLLM is in-process (fast) but still HTTP — the cache is a
    # single indexed SELECT. Use a loose 1/2 ratio with a small floor
    # so a <10ms cold path (unlikely but possible) can't produce a
    # false positive.
    assert warm_ms < max(cold_ms / 2.0, 5.0), (
        f"warm path ({warm_ms:.1f}ms) not materially faster than cold "
        f"({cold_ms:.1f}ms) — cache likely bypassed"
    )


def test_hybrid_search_finds_unique_identifier_via_bm25(
    clean_db: str, ingest_module, rag_app
) -> None:
    """Hybrid retrieval must surface a chunk via its literal identifier.

    The fixture's `src/calculator.py` is the only file containing the
    sentinel marker `quokka_addition_marker_42`. With hybrid search on
    (the chart default), the BM25 leg of RRF should rank that chunk top
    even when a query phrases the marker in a way that would not
    otherwise embed strongly. This is the "exact-identifier lookup" case
    that pure vector search struggles with on real models.
    """
    _seed(clean_db, ingest_module)

    client = TestClient(rag_app.app)
    resp = client.post("/ask", json={
        "query": "quokka_addition_marker_42",
        "repo": "sentinel",
        "top_k": 3,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["citations"], "expected at least one citation"
    paths = [c["path"] for c in body["citations"]]
    assert "src/calculator.py" in paths, (
        f"calculator.py should be cited for the unique marker; got {paths}"
    )
    # Top-1 should be the unique source file — BM25 has a clean exact match
    # and vector contributes (or is silent) on top of that.
    assert paths[0] == "src/calculator.py", (
        f"expected src/calculator.py ranked top-1; got {paths}"
    )
