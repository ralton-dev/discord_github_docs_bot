"""Unit tests for `/metrics` and the domain metrics in `app.py`.

Strategy:
- Import `app` (conftest sets the required env vars).
- Stub `_retrieve` so the handler never touches Postgres.
- Stub the OpenAI client's `embeddings.create` + `chat.completions.create`
  so the handler never hits the network.
- Drive the handler with FastAPI's TestClient.
- Assert the domain counters moved as expected, via the default global
  prometheus registry (`REGISTRY.get_sample_value`).

We rely on `prometheus_client.CONTENT_TYPE_LATEST` to identify the Prometheus
text format response header. That string looks like
`text/plain; version=0.0.4; charset=utf-8` in current client versions; the
test checks for the `text/plain` prefix to stay robust to minor version
bumps.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY


@pytest.fixture
def client():
    import app

    return TestClient(app.app)


def _stub_embedding(dim: int = 1536):
    """Return an object shaped like `client.embeddings.create(...)`."""
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.0] * dim)],
    )


def _stub_completion(
    answer: str = "the answer",
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
):
    """Return an object shaped like `client.chat.completions.create(...)`."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=answer))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _rows(n: int = 3):
    """Build fake rows matching `_retrieve`'s shape."""
    return [
        (f"src/file{i}.py", f"sha{i}" * 7, f"content {i}", "code")
        for i in range(n)
    ]


class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_200_and_prometheus_format(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        # CONTENT_TYPE_LATEST starts with "text/plain" across every
        # prometheus-client version >=0.20.
        assert resp.headers["content-type"].startswith("text/plain")
        # Our metric names MUST be present once defined (zero samples is fine
        # — the HELP/TYPE lines appear regardless).
        body = resp.text
        assert "# HELP gitdoc_queries_total" in body
        assert "# TYPE gitdoc_queries_total counter" in body
        assert "gitdoc_retrieval_hits" in body


class TestAskInstrumentsMetrics:
    def test_successful_ask_increments_queries_total_ok(self, client):
        import app

        repo = "test-metrics-ok"
        # Capture baseline to compute deltas — the registry is global and
        # other tests might have incremented labels first.
        before = REGISTRY.get_sample_value(
            "gitdoc_queries_total", {"repo": repo, "status": "ok"}
        ) or 0.0

        with (
            patch.object(app.llm.embeddings, "create", return_value=_stub_embedding()),
            patch.object(app.llm.chat.completions, "create", return_value=_stub_completion()),
            patch.object(app, "_retrieve", return_value=_rows(3)),
        ):
            resp = client.post("/ask", json={"query": "hi", "repo": repo})

        assert resp.status_code == 200
        assert resp.json()["answer"] == "the answer"

        after = REGISTRY.get_sample_value(
            "gitdoc_queries_total", {"repo": repo, "status": "ok"}
        ) or 0.0
        assert after - before == 1.0

    def test_retrieval_hits_observed_with_row_count(self, client):
        import app

        repo = "test-metrics-hits"
        # Histogram "_count" sample sums all observations — we just need to
        # confirm this request added exactly one observation bucket-side.
        before_count = REGISTRY.get_sample_value(
            "gitdoc_retrieval_hits_count", {"repo": repo}
        ) or 0.0
        # And confirm the sum incremented by exactly the row count we
        # injected (3) — histograms expose a sum too.
        before_sum = REGISTRY.get_sample_value(
            "gitdoc_retrieval_hits_sum", {"repo": repo}
        ) or 0.0

        with (
            patch.object(app.llm.embeddings, "create", return_value=_stub_embedding()),
            patch.object(app.llm.chat.completions, "create", return_value=_stub_completion()),
            patch.object(app, "_retrieve", return_value=_rows(3)),
        ):
            client.post("/ask", json={"query": "hi", "repo": repo})

        after_count = REGISTRY.get_sample_value(
            "gitdoc_retrieval_hits_count", {"repo": repo}
        ) or 0.0
        after_sum = REGISTRY.get_sample_value(
            "gitdoc_retrieval_hits_sum", {"repo": repo}
        ) or 0.0
        assert after_count - before_count == 1.0
        assert after_sum - before_sum == 3.0

    def test_empty_retrieval_sets_status_empty(self, client):
        import app

        repo = "test-metrics-empty"
        before = REGISTRY.get_sample_value(
            "gitdoc_queries_total", {"repo": repo, "status": "empty"}
        ) or 0.0

        with (
            patch.object(app.llm.embeddings, "create", return_value=_stub_embedding()),
            patch.object(app, "_retrieve", return_value=[]),
        ):
            resp = client.post("/ask", json={"query": "hi", "repo": repo})

        assert resp.status_code == 200
        assert resp.json()["citations"] == []
        after = REGISTRY.get_sample_value(
            "gitdoc_queries_total", {"repo": repo, "status": "empty"}
        ) or 0.0
        assert after - before == 1.0

    def test_token_counters_increment_from_completion_usage(self, client):
        import app

        repo = "test-metrics-tokens"
        model = app.CHAT_MODEL
        before_prompt = REGISTRY.get_sample_value(
            "gitdoc_tokens_prompt_total", {"repo": repo, "model": model}
        ) or 0.0
        before_completion = REGISTRY.get_sample_value(
            "gitdoc_tokens_completion_total", {"repo": repo, "model": model}
        ) or 0.0

        with (
            patch.object(app.llm.embeddings, "create", return_value=_stub_embedding()),
            patch.object(
                app.llm.chat.completions,
                "create",
                return_value=_stub_completion(prompt_tokens=42, completion_tokens=13),
            ),
            patch.object(app, "_retrieve", return_value=_rows(2)),
        ):
            client.post("/ask", json={"query": "hi", "repo": repo})

        after_prompt = REGISTRY.get_sample_value(
            "gitdoc_tokens_prompt_total", {"repo": repo, "model": model}
        ) or 0.0
        after_completion = REGISTRY.get_sample_value(
            "gitdoc_tokens_completion_total", {"repo": repo, "model": model}
        ) or 0.0
        assert after_prompt - before_prompt == 42.0
        assert after_completion - before_completion == 13.0

    def test_missing_usage_does_not_raise_or_increment_counters(self, client):
        """Some LiteLLM backends (e.g. Ollama) omit `usage` on the response.

        The metric-recording path must tolerate `usage=None` without bumping
        counters and without raising.
        """
        import app

        repo = "test-metrics-no-usage"
        model = app.CHAT_MODEL
        before = REGISTRY.get_sample_value(
            "gitdoc_tokens_prompt_total", {"repo": repo, "model": model}
        ) or 0.0

        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="x"))],
            usage=None,
        )
        with (
            patch.object(app.llm.embeddings, "create", return_value=_stub_embedding()),
            patch.object(app.llm.chat.completions, "create", return_value=completion),
            patch.object(app, "_retrieve", return_value=_rows(1)),
        ):
            resp = client.post("/ask", json={"query": "hi", "repo": repo})

        assert resp.status_code == 200
        after = REGISTRY.get_sample_value(
            "gitdoc_tokens_prompt_total", {"repo": repo, "model": model}
        ) or 0.0
        assert after == before  # unchanged
