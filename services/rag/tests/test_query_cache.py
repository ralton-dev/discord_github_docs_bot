"""Tests for the query cache (plan 13).

Coverage:

- ``_normalize_query`` — case / whitespace normalisation.
- ``_query_hash`` — deterministic SHA-256, normalised inputs collide,
  different inputs diverge.
- ``_latest_commit_sha`` — returns the latest ok row, None when no rows,
  cached within the 15s TTL, DB errors fall back to None.
- ``/ask`` cache hit — pre-existing row short-circuits the handler; no
  embedding or chat call is made; ``CACHE_HITS_TOTAL`` increments.
- ``/ask`` cache miss — normal path runs, INSERT issued at the end,
  ``CACHE_MISSES_TOTAL`` increments.
- Empty citations are NOT cached (the "no matches" answer path).
- ``QUERY_CACHE_ENABLED=False`` — no lookup and no insert.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import app as rag_app


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeModel:
    id: str


class _FakeModelsResp:
    def __init__(self, ids: list[str]) -> None:
        self.data = [_FakeModel(i) for i in ids]


class _FakeModelsClient:
    def list(self) -> _FakeModelsResp:
        return _FakeModelsResp(["ollama_chat/llama3.2:3b"])


class _FakeLLM:
    def __init__(self) -> None:
        self.models = _FakeModelsClient()
        self.embeddings = SimpleNamespace(create=self._embed)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._chat),
        )
        self.embed_calls = 0
        self.chat_calls = 0

    def _embed(self, *a, **kw):
        self.embed_calls += 1
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.0] * 1536)],
        )

    def _chat(self, *a, **kw):
        self.chat_calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="fresh answer"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
        )


class _FakeCursor:
    def __init__(self, row, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


class _ScriptedConn:
    """Postgres fake that scripts query responses.

    Each ``execute`` consumes the next entry from ``responses`` (a list
    of rows-or-None). The full query history is recorded in
    ``self.queries`` so tests can assert SELECT/INSERT/UPDATE SQL
    shapes without caring about exact whitespace.
    """

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple = ()) -> _FakeCursor:
        self.queries.append((query, tuple(params)))
        row = self._responses.pop(0) if self._responses else None
        return _FakeCursor(row)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caches():
    """Drop module-level caches so tests don't bleed state into each other."""
    rag_app._commit_cache.clear()
    rag_app._settings_cache.clear()
    yield
    rag_app._commit_cache.clear()
    rag_app._settings_cache.clear()


@pytest.fixture
def client():
    return TestClient(rag_app.app, raise_server_exceptions=False)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch):
    clock = _FakeClock()
    monkeypatch.setattr(rag_app, "_clock", clock)
    return clock


# ---------------------------------------------------------------------------
# _normalize_query
# ---------------------------------------------------------------------------


class TestNormalizeQuery:
    def test_lowercases(self):
        assert rag_app._normalize_query("What Is X") == "what is x"

    def test_collapses_internal_whitespace(self):
        assert rag_app._normalize_query("a    b\tc\n\nd") == "a b c d"

    def test_strips_leading_trailing_whitespace(self):
        assert rag_app._normalize_query("  hello  ") == "hello"

    def test_preserves_punctuation(self):
        # "?" is meaningful — don't accidentally drop it.
        assert rag_app._normalize_query("WHAT?") == "what?"

    def test_case_and_whitespace_variants_collide(self):
        a = rag_app._normalize_query("What is X?")
        b = rag_app._normalize_query("  what  IS  x?  ")
        assert a == b


# ---------------------------------------------------------------------------
# _query_hash
# ---------------------------------------------------------------------------


class TestQueryHash:
    def test_deterministic(self):
        assert rag_app._query_hash("hi") == rag_app._query_hash("hi")

    def test_sha256_shape(self):
        h = rag_app._query_hash("hi")
        assert len(h) == 64
        int(h, 16)  # no-op if it parses as hex

    def test_different_inputs_differ(self):
        assert rag_app._query_hash("a") != rag_app._query_hash("b")

    def test_normalized_inputs_collide(self):
        """Whitespace- and case-only differences produce the same hash."""
        assert rag_app._query_hash("What is X?") == rag_app._query_hash(
            "  what  IS  x?  "
        )


# ---------------------------------------------------------------------------
# _latest_commit_sha
# ---------------------------------------------------------------------------


class TestLatestCommitSha:
    def test_returns_latest_ok_row(self, monkeypatch, fake_clock):
        fake = _ScriptedConn(responses=[("abc123",)])
        monkeypatch.setattr(rag_app.psycopg, "connect", lambda *a, **kw: fake)
        assert rag_app._latest_commit_sha("repo") == "abc123"
        # Confirm the WHERE clause filters status='ok'.
        sql, params = fake.queries[0]
        assert "status = 'ok'" in sql
        assert params == ("repo",)

    def test_returns_none_when_no_rows(self, monkeypatch, fake_clock):
        fake = _ScriptedConn(responses=[None])
        monkeypatch.setattr(rag_app.psycopg, "connect", lambda *a, **kw: fake)
        assert rag_app._latest_commit_sha("repo") is None

    def test_cached_within_ttl(self, monkeypatch, fake_clock):
        calls = {"n": 0}

        def _connect(*a, **kw):
            calls["n"] += 1
            return _ScriptedConn(responses=[("abc123",)])

        monkeypatch.setattr(rag_app.psycopg, "connect", _connect)
        assert rag_app._latest_commit_sha("repo") == "abc123"
        assert calls["n"] == 1
        # Within the 15s TTL, no second DB call.
        fake_clock.now += 5.0
        assert rag_app._latest_commit_sha("repo") == "abc123"
        assert calls["n"] == 1

    def test_cache_expires_after_ttl(self, monkeypatch, fake_clock):
        calls = {"n": 0}

        def _connect(*a, **kw):
            calls["n"] += 1
            return _ScriptedConn(responses=[(f"sha-{calls['n']}",)])

        monkeypatch.setattr(rag_app.psycopg, "connect", _connect)
        rag_app._latest_commit_sha("repo")
        fake_clock.now += 16.0
        rag_app._latest_commit_sha("repo")
        assert calls["n"] == 2

    def test_db_error_returns_none(self, monkeypatch, fake_clock):
        def _boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(rag_app.psycopg, "connect", _boom)
        assert rag_app._latest_commit_sha("repo") is None


# ---------------------------------------------------------------------------
# /ask cache integration
# ---------------------------------------------------------------------------


def _with_cache_mode(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(rag_app, "QUERY_CACHE_ENABLED", enabled)


class _CacheStub:
    """Drive the /ask cache path with a scripted sequence of DB connects.

    Each call to ``psycopg.connect`` pops the next _ScriptedConn off
    ``conns`` and returns it. ``history`` records the popped instances so
    the test can assert which queries landed where.
    """

    def __init__(self, conns: list[_ScriptedConn]) -> None:
        self._conns = list(conns)
        self.history: list[_ScriptedConn] = []

    def __call__(self, *a, **kw):
        if not self._conns:
            raise AssertionError("no more scripted connections configured")
        c = self._conns.pop(0)
        self.history.append(c)
        return c


class TestAskCacheHit:
    def test_hit_returns_cached_answer_no_llm_calls(
        self, client, fake_clock, monkeypatch
    ):
        """Identical query with a cached row short-circuits the handler."""
        _with_cache_mode(monkeypatch, True)
        repo = "qc-hit"

        # Prime the commit-sha cache directly so the handler's first DB
        # touch is the cache lookup itself.
        rag_app._commit_cache[repo] = ("sha-current", fake_clock.now)

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)

        cached_answer = "from cache"
        cached_citations = [{"path": "src/a.py", "commit_sha": "sha-current"}]

        # One connect for SELECT + UPDATE (same transaction).
        lookup_conn = _ScriptedConn(
            responses=[(cached_answer, cached_citations), None],
        )
        stub = _CacheStub([lookup_conn])
        monkeypatch.setattr(rag_app.psycopg, "connect", stub)

        before_hits = REGISTRY.get_sample_value(
            "gitdoc_cache_hits_total", {"repo": repo}
        ) or 0.0

        resp = client.post("/ask", json={"query": "  What IS X?  ", "repo": repo})
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"] == cached_answer
        assert body["citations"] == cached_citations

        after_hits = REGISTRY.get_sample_value(
            "gitdoc_cache_hits_total", {"repo": repo}
        ) or 0.0
        assert after_hits - before_hits == 1.0

        # Embedding + chat must NOT have been called on a cache hit.
        assert fake_llm.embed_calls == 0
        assert fake_llm.chat_calls == 0

        # Exactly one DB connection was used — the SELECT+UPDATE bundle.
        assert len(stub.history) == 1
        sqls = [q[0] for q in lookup_conn.queries]
        assert any("SELECT answer, citations" in s for s in sqls)
        assert any("UPDATE query_cache" in s and "hits = hits + 1" in s for s in sqls)

    def test_normalized_query_hits_existing_row(
        self, client, fake_clock, monkeypatch
    ):
        """The hash used for lookup matches across case/whitespace variants."""
        _with_cache_mode(monkeypatch, True)
        repo = "qc-norm"
        rag_app._commit_cache[repo] = ("sha-n", fake_clock.now)

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)

        cached = [{"path": "a.py", "commit_sha": "sha-n"}]
        lookup_conn = _ScriptedConn(responses=[("ans", cached), None])
        monkeypatch.setattr(
            rag_app.psycopg, "connect", _CacheStub([lookup_conn]),
        )

        # The stored row's query_hash corresponds to "what is x?" — but
        # we send an upper-cased whitespace-padded variant.
        resp = client.post(
            "/ask", json={"query": "  What IS  X?  ", "repo": repo},
        )
        assert resp.status_code == 200
        # Confirm the SQL was called with the normalized hash.
        _, params = lookup_conn.queries[0]
        # params = (repo, commit_sha, query_hash)
        assert params[2] == rag_app._query_hash("what is x?")


class TestAskCacheMiss:
    def test_miss_runs_normal_path_and_inserts(
        self, client, fake_clock, monkeypatch
    ):
        _with_cache_mode(monkeypatch, True)
        repo = "qc-miss"
        rag_app._commit_cache[repo] = ("sha-miss", fake_clock.now)

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)
        monkeypatch.setattr(
            rag_app,
            "_retrieve",
            lambda *a, **kw: [
                ("src/f.py", "sha-miss", "contents", "code"),
            ],
        )
        # Short-circuit the settings lookup so it doesn't consume a
        # scripted connection slot.
        monkeypatch.setattr(
            rag_app, "_get_chat_model_for_repo", lambda r: "test-chat-model",
        )

        # First connect: SELECT returns None (cache miss).
        lookup_conn = _ScriptedConn(responses=[None])
        # Second connect: INSERT into query_cache after the LLM call.
        insert_conn = _ScriptedConn(responses=[None])
        stub = _CacheStub([lookup_conn, insert_conn])
        monkeypatch.setattr(rag_app.psycopg, "connect", stub)

        before_miss = REGISTRY.get_sample_value(
            "gitdoc_cache_misses_total", {"repo": repo}
        ) or 0.0

        resp = client.post("/ask", json={"query": "novel question", "repo": repo})
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"] == "fresh answer"
        assert body["citations"] == [{"path": "src/f.py", "commit_sha": "sha-miss"}]

        after_miss = REGISTRY.get_sample_value(
            "gitdoc_cache_misses_total", {"repo": repo}
        ) or 0.0
        assert after_miss - before_miss == 1.0

        # The slow path actually ran.
        assert fake_llm.embed_calls == 1
        assert fake_llm.chat_calls == 1

        # The INSERT was issued on the second connection.
        insert_sqls = [q[0] for q in insert_conn.queries]
        assert any("INSERT INTO query_cache" in s for s in insert_sqls)
        _, params = insert_conn.queries[0]
        # (repo, commit_sha, qhash, answer, citations_json)
        assert params[0] == repo
        assert params[1] == "sha-miss"
        assert params[2] == rag_app._query_hash("novel question")
        assert params[3] == "fresh answer"
        # citations serialised as a JSON array string.
        parsed = json.loads(params[4])
        assert parsed == [{"path": "src/f.py", "commit_sha": "sha-miss"}]


class TestAskCacheNoInsertOnEmpty:
    def test_empty_citations_are_not_cached(
        self, client, fake_clock, monkeypatch
    ):
        """Empty-retrieval path returns the 'no matches' answer and skips INSERT."""
        _with_cache_mode(monkeypatch, True)
        repo = "qc-empty"
        rag_app._commit_cache[repo] = ("sha-empty", fake_clock.now)

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)
        monkeypatch.setattr(rag_app, "_retrieve", lambda *a, **kw: [])
        monkeypatch.setattr(
            rag_app, "_get_chat_model_for_repo", lambda r: "test-chat-model",
        )

        # Only the cache-miss SELECT should happen. If the code issued a
        # second connect (the INSERT), _CacheStub would raise.
        lookup_conn = _ScriptedConn(responses=[None])
        monkeypatch.setattr(
            rag_app.psycopg, "connect", _CacheStub([lookup_conn]),
        )

        resp = client.post("/ask", json={"query": "nothing here", "repo": repo})
        assert resp.status_code == 200
        body = resp.json()
        assert body["citations"] == []

        # No INSERT was issued.
        assert not any(
            "INSERT INTO query_cache" in q[0] for q in lookup_conn.queries
        )


class TestAskCacheDisabled:
    def test_disabled_skips_lookup_and_insert(
        self, client, fake_clock, monkeypatch
    ):
        _with_cache_mode(monkeypatch, False)
        repo = "qc-disabled"
        # The commit-sha cache is irrelevant when the feature is off.

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)
        monkeypatch.setattr(
            rag_app,
            "_retrieve",
            lambda *a, **kw: [("src/f.py", "sha-off", "contents", "code")],
        )

        # No DB connects should happen on the cache side. The handler
        # itself does not touch the DB in the slow path (retrieval is
        # stubbed, settings cache is populated on first _get_chat_model
        # call via its own stub below).
        def _should_not_connect(*a, **kw):
            raise AssertionError(
                "psycopg.connect should not be called when cache is disabled"
            )

        monkeypatch.setattr(rag_app.psycopg, "connect", _should_not_connect)
        # Short-circuit the settings lookup — it would otherwise try to
        # connect to Postgres.
        monkeypatch.setattr(
            rag_app, "_get_chat_model_for_repo", lambda r: "test-chat-model",
        )

        resp = client.post("/ask", json={"query": "q", "repo": repo})
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"] == "fresh answer"
