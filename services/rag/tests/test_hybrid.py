"""Tests for hybrid retrieval (plan 11).

Two slices of coverage:

1. **Pure RRF math** — the fusion is a deterministic pure function. Given
   two synthetic ranked lists (no DB, no embeddings), assert the fused
   ordering matches the formula ``sum(1 / (k + rank))`` with ``k=60``.
2. **`_retrieve` dispatcher** — confirm the module-level
   ``HYBRID_SEARCH_ENABLED`` flag actually picks between the hybrid and
   vector-only paths, and only one of them is invoked per call.
"""

from __future__ import annotations

import pytest

import app as rag_app


# ---------------------------------------------------------------------------
# Pure RRF math.
# ---------------------------------------------------------------------------


class TestRrfFusion:
    def test_identical_lists_score_double(self):
        """An item appearing top in both lists scores 2 / (k + 1)."""
        fused = rag_app._rrf_fuse([["a", "b"], ["a", "b"]], k=60)
        assert fused[0][0] == "a"
        assert fused[1][0] == "b"
        assert fused[0][1] == pytest.approx(2.0 / 61)
        assert fused[1][1] == pytest.approx(2.0 / 62)

    def test_disjoint_lists_preserve_order_by_rank(self):
        """When no ID is shared, the higher-ranked-in-either-list wins."""
        # "a" rank 1 in list A, "x" rank 1 in list B → tie at 1/61.
        # "b" rank 2 in list A, "y" rank 2 in list B → tie at 1/62.
        # Tie-break is first-seen order across the input rankings.
        fused = rag_app._rrf_fuse([["a", "b"], ["x", "y"]], k=60)
        ids = [item for item, _ in fused]
        assert ids == ["a", "x", "b", "y"]
        assert fused[0][1] == pytest.approx(1.0 / 61)
        assert fused[1][1] == pytest.approx(1.0 / 61)
        assert fused[2][1] == pytest.approx(1.0 / 62)
        assert fused[3][1] == pytest.approx(1.0 / 62)

    def test_overlap_promotes_shared_item(self):
        """An item ranked mid-pack on both sides outranks a top-on-one-side item."""
        # vector: [v1, shared, v3]
        # bm25:   [b1, shared, b3]
        # shared: 1/62 + 1/62 = ~0.03226
        # v1:     1/61         = ~0.01639
        # b1:     1/61         = ~0.01639
        fused = rag_app._rrf_fuse(
            [["v1", "shared", "v3"], ["b1", "shared", "b3"]],
            k=60,
        )
        assert fused[0][0] == "shared"
        assert fused[0][1] == pytest.approx(2.0 / 62)
        # v1 was first-seen, so wins the tie against b1.
        assert fused[1][0] == "v1"
        assert fused[2][0] == "b1"

    def test_single_list_passthrough(self):
        """A single ranking fuses to the same order with scores 1/(k+rank)."""
        fused = rag_app._rrf_fuse([["a", "b", "c"]], k=60)
        assert [item for item, _ in fused] == ["a", "b", "c"]
        assert fused[0][1] == pytest.approx(1.0 / 61)
        assert fused[1][1] == pytest.approx(1.0 / 62)
        assert fused[2][1] == pytest.approx(1.0 / 63)

    def test_empty_input_returns_empty(self):
        assert rag_app._rrf_fuse([]) == []
        assert rag_app._rrf_fuse([[], []]) == []

    def test_default_k_is_60(self):
        """Sanity check: the constant is wired through."""
        assert rag_app.RRF_K == 60
        # The default-arg path uses the module RRF_K.
        fused = rag_app._rrf_fuse([["a"]])
        assert fused[0][1] == pytest.approx(1.0 / 61)


# ---------------------------------------------------------------------------
# `_retrieve` dispatcher.
# ---------------------------------------------------------------------------


class TestRetrieveDispatcher:
    def test_dispatches_to_vector_when_disabled(self, monkeypatch):
        calls = {"vector": 0, "hybrid": 0}

        def fake_vector(repo, embedding, top_k):
            calls["vector"] += 1
            return [("p", "sha", "c", "code")]

        def fake_hybrid(repo, query_text, embedding, top_k):
            calls["hybrid"] += 1
            return []

        monkeypatch.setattr(rag_app, "HYBRID_SEARCH_ENABLED", False)
        monkeypatch.setattr(rag_app, "_retrieve_vector", fake_vector)
        monkeypatch.setattr(rag_app, "_retrieve_hybrid", fake_hybrid)

        out = rag_app._retrieve("repo", "the query text", [0.1] * 4, 5)
        assert out == [("p", "sha", "c", "code")]
        assert calls == {"vector": 1, "hybrid": 0}

    def test_dispatches_to_hybrid_when_enabled(self, monkeypatch):
        calls = {"vector": 0, "hybrid": 0, "hybrid_args": None}

        def fake_vector(repo, embedding, top_k):
            calls["vector"] += 1
            return []

        def fake_hybrid(repo, query_text, embedding, top_k):
            calls["hybrid"] += 1
            calls["hybrid_args"] = (repo, query_text, top_k)
            return [("p", "sha", "c", "code")]

        monkeypatch.setattr(rag_app, "HYBRID_SEARCH_ENABLED", True)
        monkeypatch.setattr(rag_app, "_retrieve_vector", fake_vector)
        monkeypatch.setattr(rag_app, "_retrieve_hybrid", fake_hybrid)

        out = rag_app._retrieve("repo", "the query text", [0.1] * 4, 5)
        assert out == [("p", "sha", "c", "code")]
        assert calls["vector"] == 0
        assert calls["hybrid"] == 1
        # The raw query text reaches the hybrid path so the BM25 leg can
        # parse it — this is the whole point of the dispatcher signature.
        assert calls["hybrid_args"] == ("repo", "the query text", 5)
