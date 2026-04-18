"""Tests for the cross-encoder reranker (plan 12).

Two slices of coverage:

1. **`reranker.rerank` directly** — happy path + every documented
   graceful-degrade failure mode (transport error, HTTP error, malformed
   response, wrong-length scores, non-numeric scores).
2. **`/ask` dispatch** — confirm the env-var flag actually toggles the
   call, that the wider candidate pool is requested when the flag is on,
   and that the reranker output's top `top_k` flows into the LLM prompt.

The reranker call is async; we drive it with `asyncio.run` in tests rather
than declaring `async def test_…` so we don't need pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import app as rag_app
import reranker as reranker_mod


# ---------------------------------------------------------------------------
# Fake httpx client.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data=None,
        raise_on_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not JSON")
        return self._json


class _FakeClient:
    """Minimal async stub satisfying ``client.post(url, json=..., timeout=...)``."""

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.response = response
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def post(self, url, *, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _candidates(n: int = 3):
    return [
        {
            "path": f"src/file{i}.py",
            "commit_sha": f"sha{i}",
            "content": f"content body {i}",
            "content_type": "code",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# rerank() — direct tests.
# ---------------------------------------------------------------------------


class TestRerankHappyPath:
    def test_reorders_by_descending_score(self):
        cands = _candidates(3)
        client = _FakeClient(
            response=_FakeResponse(json_data={"scores": [0.2, 0.9, 0.5]}),
        )
        out = asyncio.run(
            reranker_mod.rerank(
                "what is the answer",
                cands,
                url="http://reranker.local/api/rerank",
                model="BAAI/bge-reranker-v2-m3",
                client=client,
            )
        )
        # Highest first (idx 1, then idx 2, then idx 0).
        assert [c["path"] for c in out] == [
            "src/file1.py",
            "src/file2.py",
            "src/file0.py",
        ]

    def test_payload_shape_uses_content_field(self):
        cands = _candidates(2)
        client = _FakeClient(
            response=_FakeResponse(json_data={"scores": [0.0, 0.0]}),
        )
        asyncio.run(
            reranker_mod.rerank(
                "q",
                cands,
                url="http://reranker.local",
                model="bge-x",
                client=client,
            )
        )
        assert client.calls
        body = client.calls[0]["json"]
        assert body["model"] == "bge-x"
        assert body["query"] == "q"
        assert body["documents"] == [
            {"text": "content body 0"},
            {"text": "content body 1"},
        ]

    def test_empty_candidates_short_circuits_without_http_call(self):
        client = _FakeClient(response=_FakeResponse(json_data={"scores": []}))
        out = asyncio.run(
            reranker_mod.rerank(
                "q",
                [],
                url="http://reranker.local",
                model="m",
                client=client,
            )
        )
        assert out == []
        assert client.calls == []

    def test_preserves_all_original_fields(self):
        cands = _candidates(2)
        # Add an extra arbitrary field — must survive unchanged.
        for c in cands:
            c["score_extra"] = "preserved"
        client = _FakeClient(
            response=_FakeResponse(json_data={"scores": [0.1, 0.9]}),
        )
        out = asyncio.run(
            reranker_mod.rerank(
                "q", cands, url="http://x", model="m", client=client,
            )
        )
        assert out[0]["score_extra"] == "preserved"
        assert out[0]["path"] == "src/file1.py"
        assert out[1]["path"] == "src/file0.py"


class TestRerankGracefulDegrade:
    def test_transport_error_returns_candidates_unchanged(self, caplog):
        cands = _candidates(3)
        client = _FakeClient(raise_exc=httpx.ConnectError("connection refused"))
        with caplog.at_level(logging.WARNING, logger="gitdoc.rag.reranker"):
            out = asyncio.run(
                reranker_mod.rerank(
                    "q", cands, url="http://x", model="m", client=client,
                )
            )
        assert out == cands  # unchanged
        assert any("transport error" in rec.message for rec in caplog.records)

    def test_http_error_returns_candidates_unchanged(self, caplog):
        cands = _candidates(3)
        client = _FakeClient(
            response=_FakeResponse(status_code=503, json_data={"err": "down"}),
        )
        with caplog.at_level(logging.WARNING, logger="gitdoc.rag.reranker"):
            out = asyncio.run(
                reranker_mod.rerank(
                    "q", cands, url="http://x", model="m", client=client,
                )
            )
        assert out == cands
        assert any("HTTP 503" in rec.message for rec in caplog.records)

    def test_malformed_response_missing_scores_key(self, caplog):
        cands = _candidates(3)
        client = _FakeClient(
            response=_FakeResponse(json_data={"results": [1, 2, 3]}),
        )
        with caplog.at_level(logging.WARNING, logger="gitdoc.rag.reranker"):
            out = asyncio.run(
                reranker_mod.rerank(
                    "q", cands, url="http://x", model="m", client=client,
                )
            )
        assert out == cands
        assert any("malformed" in rec.message for rec in caplog.records)

    def test_malformed_response_wrong_length(self, caplog):
        cands = _candidates(3)
        # One score for three documents — must be rejected.
        client = _FakeClient(
            response=_FakeResponse(json_data={"scores": [0.5]}),
        )
        with caplog.at_level(logging.WARNING, logger="gitdoc.rag.reranker"):
            out = asyncio.run(
                reranker_mod.rerank(
                    "q", cands, url="http://x", model="m", client=client,
                )
            )
        assert out == cands
        assert any("malformed" in rec.message for rec in caplog.records)

    def test_non_numeric_scores_returns_candidates_unchanged(self, caplog):
        cands = _candidates(2)
        client = _FakeClient(
            response=_FakeResponse(json_data={"scores": ["high", "low"]}),
        )
        with caplog.at_level(logging.WARNING, logger="gitdoc.rag.reranker"):
            out = asyncio.run(
                reranker_mod.rerank(
                    "q", cands, url="http://x", model="m", client=client,
                )
            )
        assert out == cands
        assert any("not numeric" in rec.message for rec in caplog.records)

    def test_non_json_body_returns_candidates_unchanged(self, caplog):
        cands = _candidates(2)
        client = _FakeClient(
            response=_FakeResponse(raise_on_json=True),
        )
        with caplog.at_level(logging.WARNING, logger="gitdoc.rag.reranker"):
            out = asyncio.run(
                reranker_mod.rerank(
                    "q", cands, url="http://x", model="m", client=client,
                )
            )
        assert out == cands
        assert any("non-JSON" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# /ask dispatch — env flag toggling.
# ---------------------------------------------------------------------------


def _stub_embedding(dim: int = 1536):
    return SimpleNamespace(data=[SimpleNamespace(embedding=[0.0] * dim)])


def _stub_completion(answer: str = "ok"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=answer))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


def _rows(n: int):
    """Mimic the tuple shape of `_retrieve` rows."""
    return [
        (f"src/file{i}.py", f"sha{i}" * 7, f"content {i}", "code")
        for i in range(n)
    ]


@pytest.fixture
def client():
    return TestClient(rag_app.app)


class TestAskRerankDispatch:
    def test_disabled_skips_rerank_call(self, client, monkeypatch):
        """When RERANKER_ENABLED is False, no rerank invocation happens."""
        called = {"n": 0}

        async def sentinel_rerank(*args, **kwargs):  # pragma: no cover - guarded
            called["n"] += 1
            return list(args[1])

        monkeypatch.setattr(rag_app, "RERANKER_ENABLED", False)
        monkeypatch.setattr(rag_app, "RERANKER_URL", "http://reranker.local")
        monkeypatch.setattr(reranker_mod, "rerank", sentinel_rerank)

        captured = {"top_k": None}

        def fake_retrieve(repo, query, emb, top_k):
            captured["top_k"] = top_k
            return _rows(3)

        with (
            patch.object(rag_app, "_retrieve", side_effect=fake_retrieve),
            patch.object(
                rag_app.llm.embeddings, "create", return_value=_stub_embedding(),
            ),
            patch.object(
                rag_app.llm.chat.completions, "create", return_value=_stub_completion(),
            ),
        ):
            resp = client.post(
                "/ask", json={"query": "hi", "repo": "test-rerank-off", "top_k": 3},
            )

        assert resp.status_code == 200
        assert called["n"] == 0
        # Disabled => retrieve is asked for the literal top_k.
        assert captured["top_k"] == 3

    def test_disabled_when_url_empty(self, client, monkeypatch):
        """Even with the flag on, an empty URL skips rerank (defensive default)."""
        called = {"n": 0}

        async def sentinel_rerank(*args, **kwargs):  # pragma: no cover
            called["n"] += 1
            return list(args[1])

        monkeypatch.setattr(rag_app, "RERANKER_ENABLED", True)
        monkeypatch.setattr(rag_app, "RERANKER_URL", "")
        monkeypatch.setattr(reranker_mod, "rerank", sentinel_rerank)

        with (
            patch.object(rag_app, "_retrieve", return_value=_rows(2)),
            patch.object(
                rag_app.llm.embeddings, "create", return_value=_stub_embedding(),
            ),
            patch.object(
                rag_app.llm.chat.completions, "create", return_value=_stub_completion(),
            ),
        ):
            resp = client.post(
                "/ask", json={"query": "hi", "repo": "test-rerank-no-url"},
            )
        assert resp.status_code == 200
        assert called["n"] == 0

    def test_enabled_widens_retrieve_and_trims_to_top_k(self, client, monkeypatch):
        """When enabled, retrieve is called with top_k * MULT and rerank trims."""
        captured = {"retrieve_top_k": None, "rerank_inputs": None}

        # Build 9 rows so top_k=3 * MULT=3 hits the wide-pool path.
        wide_rows = _rows(9)

        def fake_retrieve(repo, query, emb, top_k):
            captured["retrieve_top_k"] = top_k
            return wide_rows

        async def fake_rerank(query, candidates, *, url, model, **kw):
            captured["rerank_inputs"] = list(candidates)
            # Reverse the order — the trim should keep the *new* top 3,
            # which corresponds to file8/file7/file6 in the original list.
            return list(reversed(candidates))

        # Patch the chat call so we can assert what reached the LLM.
        sent_messages = {"value": None}

        def fake_chat(*, model, messages, temperature, max_tokens):
            sent_messages["value"] = messages
            return _stub_completion()

        monkeypatch.setattr(rag_app, "RERANKER_ENABLED", True)
        monkeypatch.setattr(rag_app, "RERANKER_URL", "http://reranker.local")
        monkeypatch.setattr(rag_app, "RERANKER_MULT", 3)
        monkeypatch.setattr(reranker_mod, "rerank", fake_rerank)

        with (
            patch.object(rag_app, "_retrieve", side_effect=fake_retrieve),
            patch.object(
                rag_app.llm.embeddings, "create", return_value=_stub_embedding(),
            ),
            patch.object(
                rag_app.llm.chat.completions, "create", side_effect=fake_chat,
            ),
        ):
            resp = client.post(
                "/ask",
                json={"query": "hi", "repo": "test-rerank-on", "top_k": 3},
            )

        assert resp.status_code == 200
        # Retrieve was widened.
        assert captured["retrieve_top_k"] == 9
        # Reranker was given all 9 candidates as dicts with content fields.
        assert captured["rerank_inputs"] is not None
        assert len(captured["rerank_inputs"]) == 9
        assert captured["rerank_inputs"][0]["content"] == "content 0"

        # Top-3 (after reverse) should be file8 / file7 / file6.
        body = resp.json()
        cited_paths = [c["path"] for c in body["citations"]]
        assert cited_paths == ["src/file8.py", "src/file7.py", "src/file6.py"]

        # And the LLM prompt should reflect the trimmed, reranked rows.
        assert sent_messages["value"] is not None
        user_prompt = sent_messages["value"][1]["content"]
        assert "src/file8.py" in user_prompt
        assert "src/file7.py" in user_prompt
        assert "src/file6.py" in user_prompt
        # File 0 was at the bottom of the reversed pool — it should NOT
        # appear in the trimmed top_k=3 sent to the LLM.
        assert "src/file0.py" not in user_prompt

    def test_rerank_failure_falls_through_with_unranked_rows(self, client, monkeypatch):
        """A reranker exception escaping `rerank()` must not break /ask."""

        async def explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(rag_app, "RERANKER_ENABLED", True)
        monkeypatch.setattr(rag_app, "RERANKER_URL", "http://reranker.local")
        monkeypatch.setattr(rag_app, "RERANKER_MULT", 2)
        monkeypatch.setattr(reranker_mod, "rerank", explode)

        with (
            patch.object(rag_app, "_retrieve", return_value=_rows(4)),
            patch.object(
                rag_app.llm.embeddings, "create", return_value=_stub_embedding(),
            ),
            patch.object(
                rag_app.llm.chat.completions, "create", return_value=_stub_completion(),
            ),
        ):
            resp = client.post(
                "/ask",
                json={"query": "hi", "repo": "test-rerank-fail", "top_k": 2},
            )
        assert resp.status_code == 200
        # Top-2 of the un-reranked rows should land in citations.
        body = resp.json()
        assert [c["path"] for c in body["citations"]] == [
            "src/file0.py", "src/file1.py",
        ]
