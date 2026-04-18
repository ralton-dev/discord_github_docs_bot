"""Tests for runtime model selection (plan 17).

Covers:

- the in-process /models cache (injectable clock so TTL is deterministic),
- GET /settings null-row and present-row shapes,
- POST /settings upsert + validation against the cached model list,
- the /ask model-resolution path (DB override vs env-var default).

No real Postgres, no real LiteLLM. ``app.llm`` and ``psycopg.connect`` are
monkeypatched per-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as rag_app

# This whole module exercises the real `_get_chat_model_for_repo` path,
# so opt out of the autouse fixture in conftest.py that would otherwise
# replace it with a stub.
pytestmark = pytest.mark.no_autoset_model


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
    def __init__(self, ids: list[str]) -> None:
        self._ids = ids
        self.calls = 0

    def list(self) -> _FakeModelsResp:
        self.calls += 1
        return _FakeModelsResp(self._ids)


class _FakeLLM:
    def __init__(self, ids: list[str]) -> None:
        self.models = _FakeModelsClient(ids)


class _FakeCursor:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def fetchone(self) -> tuple | None:
        return self._row


class _FakeConn:
    """Script a sequence of rows to return from ``conn.execute(...).fetchone()``.

    Each call consumes the next row in ``rows``. If ``rows`` is exhausted the
    last row is returned (useful for upserts that read back the written row).
    """

    def __init__(self, rows: list[tuple | None]) -> None:
        self._rows = list(rows)
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, query: str, params: tuple = ()) -> _FakeCursor:
        self.queries.append((query, tuple(params)))
        row = self._rows.pop(0) if self._rows else None
        return _FakeCursor(row)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caches():
    """Drop the module-level caches so tests don't bleed state into each other."""
    rag_app._models_cache["data"] = []
    rag_app._models_cache["fetched_at"] = -1e18
    rag_app._settings_cache.clear()
    yield
    rag_app._models_cache["data"] = []
    rag_app._models_cache["fetched_at"] = -1e18
    rag_app._settings_cache.clear()


@pytest.fixture
def client():
    return TestClient(rag_app.app, raise_server_exceptions=False)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeLLM(["ollama_chat/llama3.2:3b", "gpt-4o-mini", "claude-opus-4-7"])
    monkeypatch.setattr(rag_app, "llm", fake)
    return fake


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch):
    clock = _FakeClock(now=1000.0)
    monkeypatch.setattr(rag_app, "_clock", clock)
    return clock


# ---------------------------------------------------------------------------
# /models cache
# ---------------------------------------------------------------------------


class TestModelsEndpoint:
    def test_returns_list_from_litellm(self, client, fake_llm, fake_clock):
        r = client.get("/models")
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()["data"]]
        assert ids == ["ollama_chat/llama3.2:3b", "gpt-4o-mini", "claude-opus-4-7"]

    def test_cache_hit_within_ttl_does_not_refetch(self, client, fake_llm, fake_clock):
        client.get("/models")
        assert fake_llm.models.calls == 1
        # Advance within the TTL (60s default).
        fake_clock.now += 30.0
        client.get("/models")
        assert fake_llm.models.calls == 1  # still one — cache hit

    def test_cache_expires_after_ttl(self, client, fake_llm, fake_clock):
        client.get("/models")
        assert fake_llm.models.calls == 1
        fake_clock.now += 61.0
        client.get("/models")
        assert fake_llm.models.calls == 2  # refetched


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------


class TestGetSettings:
    def test_missing_row_returns_null_chat_model(self, client, monkeypatch):
        fake = _FakeConn(rows=[None])
        monkeypatch.setattr(rag_app.psycopg, "connect", lambda *a, **kw: fake)
        r = client.get("/settings", params={"repo": "project_a"})
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "repo": "project_a",
            "chat_model": None,
            "updated_at": None,
            "updated_by": None,
        }

    def test_present_row_returned(self, client, monkeypatch):
        import datetime as _dt

        ts = _dt.datetime(2026, 4, 18, 12, 0, 0, tzinfo=_dt.timezone.utc)
        fake = _FakeConn(rows=[("gpt-4o-mini", ts, "123456789012345678")])
        monkeypatch.setattr(rag_app.psycopg, "connect", lambda *a, **kw: fake)
        r = client.get("/settings", params={"repo": "project_a"})
        assert r.status_code == 200
        body = r.json()
        assert body["chat_model"] == "gpt-4o-mini"
        assert body["updated_by"] == "123456789012345678"
        assert body["updated_at"].startswith("2026-04-18")

    def test_missing_repo_param_400(self, client):
        r = client.get("/settings", params={"repo": ""})
        assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# POST /settings
# ---------------------------------------------------------------------------


class TestPostSettings:
    def test_valid_model_upserts_and_returns_row(
        self, client, fake_llm, fake_clock, monkeypatch
    ):
        import datetime as _dt

        ts = _dt.datetime(2026, 4, 18, 13, 0, 0, tzinfo=_dt.timezone.utc)
        fake = _FakeConn(rows=[("gpt-4o-mini", ts, "111")])
        monkeypatch.setattr(rag_app.psycopg, "connect", lambda *a, **kw: fake)
        r = client.post(
            "/settings",
            json={
                "repo": "project_a",
                "chat_model": "gpt-4o-mini",
                "updated_by": "111",
            },
        )
        assert r.status_code == 200
        assert r.json()["chat_model"] == "gpt-4o-mini"
        # Upsert SQL was issued.
        assert any("INSERT INTO instance_settings" in q[0] for q in fake.queries)

    def test_unknown_model_400_with_available_list(
        self, client, fake_llm, fake_clock, monkeypatch
    ):
        # No DB call should be made — validation happens first.
        def _should_not_connect(*a, **kw):
            raise AssertionError("db connect should not be called on invalid model")

        monkeypatch.setattr(rag_app.psycopg, "connect", _should_not_connect)
        r = client.post(
            "/settings",
            json={"repo": "project_a", "chat_model": "does-not-exist", "updated_by": "x"},
        )
        assert r.status_code == 400
        body = r.json()
        assert "does-not-exist" in body["error"]
        assert "gpt-4o-mini" in body["available"]

    def test_upsert_invalidates_settings_cache(
        self, client, fake_llm, fake_clock, monkeypatch
    ):
        import datetime as _dt

        # Prime cache with an old value.
        rag_app._settings_cache["project_a"] = ("old-model", fake_clock.now)

        ts = _dt.datetime(2026, 4, 18, 13, 0, 0, tzinfo=_dt.timezone.utc)
        fake = _FakeConn(rows=[("gpt-4o-mini", ts, "111")])
        monkeypatch.setattr(rag_app.psycopg, "connect", lambda *a, **kw: fake)
        client.post(
            "/settings",
            json={"repo": "project_a", "chat_model": "gpt-4o-mini", "updated_by": "111"},
        )
        # Cache dropped — next /ask will re-read the DB.
        assert "project_a" not in rag_app._settings_cache


# ---------------------------------------------------------------------------
# /ask model resolution
# ---------------------------------------------------------------------------


class TestResolveChatModelForRepo:
    def test_returns_none_when_row_missing(self, monkeypatch, fake_clock):
        fake = _FakeConn(rows=[None])
        monkeypatch.setattr(rag_app.psycopg, "connect", lambda *a, **kw: fake)
        # No env fallback: a missing row surfaces as None so callers can
        # tell the user "run /model set" instead of silently routing to
        # some default that may not exist on the backend.
        assert rag_app._get_chat_model_for_repo("project_a") is None

    def test_returns_override_when_present(self, monkeypatch, fake_clock):
        fake = _FakeConn(rows=[("gpt-4o-mini",)])
        monkeypatch.setattr(rag_app.psycopg, "connect", lambda *a, **kw: fake)
        assert rag_app._get_chat_model_for_repo("project_a") == "gpt-4o-mini"

    def test_cache_hit_within_ttl(self, monkeypatch, fake_clock):
        calls = {"n": 0}

        def _conn(*a, **kw):
            calls["n"] += 1
            return _FakeConn(rows=[("gpt-4o-mini",)])

        monkeypatch.setattr(rag_app.psycopg, "connect", _conn)
        rag_app._get_chat_model_for_repo("project_a")
        assert calls["n"] == 1
        fake_clock.now += 5.0  # within 15s TTL
        rag_app._get_chat_model_for_repo("project_a")
        assert calls["n"] == 1  # cache hit

    def test_cache_expires_after_ttl(self, monkeypatch, fake_clock):
        calls = {"n": 0}

        def _conn(*a, **kw):
            calls["n"] += 1
            return _FakeConn(rows=[("gpt-4o-mini",)])

        monkeypatch.setattr(rag_app.psycopg, "connect", _conn)
        rag_app._get_chat_model_for_repo("project_a")
        fake_clock.now += 20.0
        rag_app._get_chat_model_for_repo("project_a")
        assert calls["n"] == 2

    def test_db_error_returns_none(self, monkeypatch):
        def _blow_up(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(rag_app.psycopg, "connect", _blow_up)
        # DB error also returns None — surfacing "not set" is better than
        # silently routing to a stale env default that might not be what
        # the operator meant.
        assert rag_app._get_chat_model_for_repo("project_a") is None


# ---------------------------------------------------------------------------
# /ask short-circuits on unset model
# ---------------------------------------------------------------------------


class TestAskRequiresModel:
    def test_ask_returns_409_when_model_not_set(
        self, client, fake_clock, monkeypatch
    ):
        # No override in the DB and no env default fallback — /ask should
        # return 409 with a structured body the bot can detect.
        fake = _FakeConn(rows=[None])
        monkeypatch.setattr(rag_app.psycopg, "connect", lambda *a, **kw: fake)
        # Skip rate limit and query-cache fast paths by disabling them.
        monkeypatch.setattr(rag_app, "RATE_LIMIT_ENABLED", False)
        monkeypatch.setattr(rag_app, "QUERY_CACHE_ENABLED", False)

        r = client.post(
            "/ask",
            json={"query": "anything", "repo": "project_a", "top_k": 3},
        )
        assert r.status_code == 409
        body = r.json()
        assert body["error"] == "model not set"
        assert "/model set" in body["action"]
