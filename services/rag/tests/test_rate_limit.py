"""Tests for rate limiting & cost caps (plan 14).

Coverage:

- ``_rate_limit_check`` with a scripted DB:
  - No counts → allowed, reason None.
  - Over guild cap → blocked, reason ``guild_budget``, retry_after > 0.
  - Over user cap (but under guild) → blocked, reason ``user_budget``.
  - Both ids None → allowed, skipped (no DB touch).
  - DB error → allowed (graceful degrade).
- ``_record_rate_limit_usage`` issues an INSERT with the expected shape
  and swallows DB errors.
- ``AskRequest`` Pydantic shape: ``guild_id`` / ``user_id`` default None;
  strings accepted.
- ``/ask`` handler:
  - Over-budget request returns 429 with ``{"error", "retry_after"}`` and
    bumps the rate-limit hits counter; never touches cache or LLM.
  - Under-budget request proceeds; records usage at the end using
    ``prompt + completion`` tokens.
  - Cache-hit path still records usage but with ``tokens=0`` (cached
    answers are free).
  - Rate-limit gate runs BEFORE the query-cache lookup (rate-limited
    request must not consume a cache hit either).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import app as rag_app


# ---------------------------------------------------------------------------
# Fakes (kept compatible with the shape used by test_query_cache).
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
            choices=[SimpleNamespace(message=SimpleNamespace(content="an answer"))],
            usage=SimpleNamespace(prompt_tokens=42, completion_tokens=8),
        )


class _FakeCursor:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _ScriptedConn:
    """Scripted Postgres fake — pops a row per execute, records queries."""

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


class _ConnStub:
    """Drives ``psycopg.connect`` with a scripted sequence of connections."""

    def __init__(self, conns: list[_ScriptedConn]) -> None:
        self._conns = list(conns)
        self.history: list[_ScriptedConn] = []

    def __call__(self, *a, **kw):
        if not self._conns:
            raise AssertionError("no more scripted connections configured")
        c = self._conns.pop(0)
        self.history.append(c)
        return c


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caches():
    rag_app._commit_cache.clear()
    rag_app._settings_cache.clear()
    yield
    rag_app._commit_cache.clear()
    rag_app._settings_cache.clear()


@pytest.fixture
def client():
    return TestClient(rag_app.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# _rate_limit_check
# ---------------------------------------------------------------------------


class TestRateLimitCheck:
    def test_allows_when_no_identity(self, monkeypatch):
        # Both ids None => skip DB entirely.
        def _should_not_connect(*a, **kw):
            raise AssertionError(
                "rate_limit_check must not hit the DB when both ids are None"
            )

        monkeypatch.setattr(rag_app.psycopg, "connect", _should_not_connect)
        allowed, reason, retry = rag_app._rate_limit_check(None, None)
        assert allowed is True
        assert reason is None
        assert retry == 0

    def test_allows_when_both_buckets_under_cap(self, monkeypatch):
        # Both SELECTs come back with 0 tokens used.
        conn = _ScriptedConn(responses=[(0,), (0,)])
        monkeypatch.setattr(
            rag_app.psycopg, "connect", _ConnStub([conn]),
        )
        allowed, reason, retry = rag_app._rate_limit_check("G", "U")
        assert allowed is True
        assert reason is None
        assert retry == 0
        # Two SELECTs hit the DB — one per bucket.
        assert len(conn.queries) == 2
        assert "guild_id" in conn.queries[0][0]
        assert "user_id" in conn.queries[1][0]

    def test_blocks_on_guild_over_cap(self, monkeypatch):
        over = rag_app.GUILD_TOKENS_PER_HOUR + 1
        # Responses: guild SUM, retry_after MIN-age query.
        conn = _ScriptedConn(responses=[(over,), (1800.0,)])
        monkeypatch.setattr(
            rag_app.psycopg, "connect", _ConnStub([conn]),
        )
        allowed, reason, retry = rag_app._rate_limit_check("G", "U")
        assert allowed is False
        assert reason == "guild_budget"
        assert retry > 0
        # retry_after_secs = max(1, 3600 - 1800) = 1800
        assert retry == 1800

    def test_blocks_on_user_over_cap_when_guild_under(self, monkeypatch):
        over = rag_app.USER_TOKENS_PER_HOUR + 1
        # guild under, user over, then retry_after query.
        conn = _ScriptedConn(responses=[(0,), (over,), (900.0,)])
        monkeypatch.setattr(
            rag_app.psycopg, "connect", _ConnStub([conn]),
        )
        allowed, reason, retry = rag_app._rate_limit_check("G", "U")
        assert allowed is False
        assert reason == "user_budget"
        # 3600 - 900 = 2700
        assert retry == 2700

    def test_graceful_degrade_on_db_error(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(rag_app.psycopg, "connect", _boom)
        allowed, reason, retry = rag_app._rate_limit_check("G", "U")
        assert allowed is True
        assert reason is None
        assert retry == 0

    def test_retry_after_minimum_is_one(self, monkeypatch):
        """Near-full-window ageing must still return retry_after >= 1."""
        over = rag_app.GUILD_TOKENS_PER_HOUR + 1
        # 3600 age -> 3600 - 3600 = 0; clamp to 1.
        conn = _ScriptedConn(responses=[(over,), (3600.0,)])
        monkeypatch.setattr(
            rag_app.psycopg, "connect", _ConnStub([conn]),
        )
        _, _, retry = rag_app._rate_limit_check("G", "U")
        assert retry >= 1


# ---------------------------------------------------------------------------
# _record_rate_limit_usage
# ---------------------------------------------------------------------------


class TestRecordRateLimitUsage:
    def test_insert_shape(self, monkeypatch):
        conn = _ScriptedConn(responses=[None])
        monkeypatch.setattr(
            rag_app.psycopg, "connect", _ConnStub([conn]),
        )
        rag_app._record_rate_limit_usage("G1", "U1", "repo-a", 123)
        assert len(conn.queries) == 1
        sql, params = conn.queries[0]
        assert "INSERT INTO rate_limit_usage" in sql
        assert params == ("G1", "U1", "repo-a", 123)

    def test_skips_when_both_ids_none(self, monkeypatch):
        def _should_not_connect(*a, **kw):
            raise AssertionError("no insert when both ids are None")

        monkeypatch.setattr(rag_app.psycopg, "connect", _should_not_connect)
        rag_app._record_rate_limit_usage(None, None, "r", 100)

    def test_db_error_is_swallowed(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(rag_app.psycopg, "connect", _boom)
        # Must not raise.
        rag_app._record_rate_limit_usage("G", "U", "r", 50)


# ---------------------------------------------------------------------------
# AskRequest pydantic shape
# ---------------------------------------------------------------------------


class TestAskRequestShape:
    def test_defaults_to_none(self):
        req = rag_app.AskRequest(query="q", repo="r")
        assert req.guild_id is None
        assert req.user_id is None

    def test_accepts_strings(self):
        req = rag_app.AskRequest(
            query="q", repo="r", guild_id="123", user_id="456",
        )
        assert req.guild_id == "123"
        assert req.user_id == "456"


# ---------------------------------------------------------------------------
# /ask integration — over-budget returns 429
# ---------------------------------------------------------------------------


class TestAskOverBudget:
    def test_returns_429_and_bumps_metric(self, client, monkeypatch):
        monkeypatch.setattr(rag_app, "RATE_LIMIT_ENABLED", True)
        # Turn query cache off so the only DB plumbing in play is
        # rate-limit related and the test is explicit about ordering.
        monkeypatch.setattr(rag_app, "QUERY_CACHE_ENABLED", False)
        repo = "rl-over"

        over = rag_app.GUILD_TOKENS_PER_HOUR + 1
        # One _rate_limit_check connection: SUM (over), MIN age.
        check_conn = _ScriptedConn(responses=[(over,), (600.0,)])
        stub = _ConnStub([check_conn])
        monkeypatch.setattr(rag_app.psycopg, "connect", stub)

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)

        before = REGISTRY.get_sample_value(
            "gitdoc_rate_limit_hits_total",
            {"repo": repo, "reason": "guild_budget"},
        ) or 0.0

        resp = client.post(
            "/ask",
            json={
                "query": "anything",
                "repo": repo,
                "guild_id": "G",
                "user_id": "U",
            },
        )
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "guild_budget"
        assert body["retry_after"] == 3000  # 3600 - 600

        after = REGISTRY.get_sample_value(
            "gitdoc_rate_limit_hits_total",
            {"repo": repo, "reason": "guild_budget"},
        ) or 0.0
        assert after - before == 1.0

        # Rate-limited request must NOT have consumed the LLM.
        assert fake_llm.embed_calls == 0
        assert fake_llm.chat_calls == 0
        # Only the rate-limit check connection was opened — no cache
        # lookup, no usage INSERT (429 path doesn't record usage).
        assert len(stub.history) == 1


class TestAskUnderBudget:
    def test_proceeds_and_records_usage(self, client, monkeypatch):
        monkeypatch.setattr(rag_app, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(rag_app, "QUERY_CACHE_ENABLED", False)
        repo = "rl-under"

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)
        monkeypatch.setattr(
            rag_app,
            "_retrieve",
            lambda *a, **kw: [("src/f.py", "sha-u", "contents", "code")],
        )
        monkeypatch.setattr(
            rag_app, "_get_chat_model_for_repo", lambda r: "test-chat-model",
        )

        # Rate-limit check: two SUM queries returning 0 each.
        check_conn = _ScriptedConn(responses=[(0,), (0,)])
        # Usage INSERT after successful LLM call.
        insert_conn = _ScriptedConn(responses=[None])
        stub = _ConnStub([check_conn, insert_conn])
        monkeypatch.setattr(rag_app.psycopg, "connect", stub)

        resp = client.post(
            "/ask",
            json={
                "query": "hello",
                "repo": repo,
                "guild_id": "G",
                "user_id": "U",
            },
        )
        assert resp.status_code == 200, resp.text
        assert fake_llm.chat_calls == 1

        # The INSERT recorded prompt+completion tokens (42+8).
        insert_sqls = [q[0] for q in insert_conn.queries]
        assert any("INSERT INTO rate_limit_usage" in s for s in insert_sqls)
        _, params = insert_conn.queries[0]
        assert params == ("G", "U", repo, 50)

    def test_disabled_skips_check_and_insert(self, client, monkeypatch):
        monkeypatch.setattr(rag_app, "RATE_LIMIT_ENABLED", False)
        monkeypatch.setattr(rag_app, "QUERY_CACHE_ENABLED", False)
        repo = "rl-off"

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)
        monkeypatch.setattr(
            rag_app,
            "_retrieve",
            lambda *a, **kw: [("src/f.py", "sha-off", "contents", "code")],
        )
        monkeypatch.setattr(
            rag_app, "_get_chat_model_for_repo", lambda r: "test-chat-model",
        )

        # No DB connections should be made.
        def _should_not_connect(*a, **kw):
            raise AssertionError(
                "RATE_LIMIT_ENABLED=False must not touch the DB"
            )

        monkeypatch.setattr(rag_app.psycopg, "connect", _should_not_connect)

        resp = client.post(
            "/ask",
            json={
                "query": "hello",
                "repo": repo,
                "guild_id": "G",
                "user_id": "U",
            },
        )
        assert resp.status_code == 200, resp.text


class TestAskCacheHitStillRecordsZeroUsage:
    """Cache-hit path records usage with tokens=0 (cached = free)."""

    def test_cache_hit_records_zero_tokens(self, client, monkeypatch):
        monkeypatch.setattr(rag_app, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(rag_app, "QUERY_CACHE_ENABLED", True)

        repo = "rl-cache"
        # Seed the commit-sha cache directly so the handler proceeds
        # straight to cache lookup without a DB hop of its own.
        rag_app._commit_cache[repo] = ("sha-current", rag_app._clock())

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)

        cached_answer = "from cache"
        cached_citations = [{"path": "x.py", "commit_sha": "sha-current"}]

        # 1) rate-limit check: two SUMs under cap.
        check_conn = _ScriptedConn(responses=[(0,), (0,)])
        # 2) cache lookup: SELECT + UPDATE.
        lookup_conn = _ScriptedConn(
            responses=[(cached_answer, cached_citations), None],
        )
        # 3) rate-limit usage INSERT (tokens=0).
        insert_conn = _ScriptedConn(responses=[None])
        stub = _ConnStub([check_conn, lookup_conn, insert_conn])
        monkeypatch.setattr(rag_app.psycopg, "connect", stub)

        resp = client.post(
            "/ask",
            json={
                "query": "  What IS X?  ",
                "repo": repo,
                "guild_id": "G",
                "user_id": "U",
            },
        )
        assert resp.status_code == 200, resp.text
        assert fake_llm.embed_calls == 0
        assert fake_llm.chat_calls == 0

        _, params = insert_conn.queries[0]
        # Cache hit ⇒ tokens=0 (cached answers don't count against budget).
        assert params == ("G", "U", repo, 0)


class TestAskGateRunsBeforeCache:
    """A 429'd request must NOT touch the query cache."""

    def test_over_budget_does_not_hit_cache(self, client, monkeypatch):
        monkeypatch.setattr(rag_app, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(rag_app, "QUERY_CACHE_ENABLED", True)

        repo = "rl-gate"
        # Pre-populate commit-sha cache so the handler would have gone
        # straight to the cache SELECT if the gate didn't fire.
        rag_app._commit_cache[repo] = ("sha-x", rag_app._clock())

        over = rag_app.GUILD_TOKENS_PER_HOUR + 1
        # Only the rate-limit check connection should be consumed.
        check_conn = _ScriptedConn(responses=[(over,), (60.0,)])
        stub = _ConnStub([check_conn])
        monkeypatch.setattr(rag_app.psycopg, "connect", stub)

        fake_llm = _FakeLLM()
        monkeypatch.setattr(rag_app, "llm", fake_llm)

        resp = client.post(
            "/ask",
            json={
                "query": "q",
                "repo": repo,
                "guild_id": "G",
                "user_id": "U",
            },
        )
        assert resp.status_code == 429
        # Zero cache lookups issued — the gate ran first.
        assert len(stub.history) == 1
        assert not any(
            "SELECT answer, citations" in q[0]
            for q in check_conn.queries
        )
