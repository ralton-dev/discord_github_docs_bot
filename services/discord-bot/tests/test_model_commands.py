"""Tests for the /model slash command helpers (plan 17).

Covers the three httpx round-trip helpers end-to-end and the permission
gate for /model set. Uses ``httpx.MockTransport`` (no real network, no
respx dependency) and a tiny fake interaction to drive the handlers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import bot


# ---------------------------------------------------------------------------
# Helpers — sync wrapper + AsyncClient patching
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _ClientFactory:
    """Drop-in replacement for httpx.AsyncClient that uses a MockTransport.

    The signature matches `httpx.AsyncClient(timeout=...)` so bot.py's
    existing `async with httpx.AsyncClient(timeout=...)` pattern picks this
    up when we monkeypatch the module attribute. Captures the real
    ``httpx.AsyncClient`` class at import time so the factory's internal
    construction does not re-enter the monkeypatched name.
    """

    def __init__(self, handler):
        self._handler = handler

    def __call__(self, *a, **kw):
        transport = httpx.MockTransport(self._handler)
        return _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _reset_caches():
    bot._models_bot_cache["ids"] = []
    bot._models_bot_cache["fetched_at"] = -1e18
    # Also reset the history-unsupported latch so log counting stays clean
    # between tests.
    bot._history_unsupported_logged = False
    yield


# ---------------------------------------------------------------------------
# _fetch_models
# ---------------------------------------------------------------------------


class TestFetchModels:
    def test_returns_ids_from_orchestrator(self, monkeypatch):
        captured: dict[str, httpx.Request] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["req"] = req
            return httpx.Response(
                200,
                json={"data": [{"id": "gpt-4o-mini"}, {"id": "claude-opus-4-7"}]},
            )

        monkeypatch.setattr(bot, "httpx", httpx)
        monkeypatch.setattr(bot.httpx, "AsyncClient", _ClientFactory(handler))
        # URL passed in is ignored by the MockTransport (it dispatches by handler)
        ids = _run(bot._fetch_models("http://test"))
        assert ids == ["gpt-4o-mini", "claude-opus-4-7"]
        assert captured["req"].url.path == "/models"

    def test_cache_hit_avoids_second_http_call(self, monkeypatch):
        n = {"calls": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            n["calls"] += 1
            return httpx.Response(200, json={"data": [{"id": "a"}]})

        monkeypatch.setattr(bot.httpx, "AsyncClient", _ClientFactory(handler))
        _run(bot._fetch_models("http://test"))
        _run(bot._fetch_models("http://test"))
        assert n["calls"] == 1  # cache hit on second call


# ---------------------------------------------------------------------------
# _fetch_current
# ---------------------------------------------------------------------------


class TestFetchCurrent:
    def test_passes_repo_query_param(self, monkeypatch):
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = req.url
            return httpx.Response(200, json={
                "repo": "project_a",
                "chat_model": "gpt-4o-mini",
                "updated_at": "2026-04-18T12:00:00Z",
                "updated_by": "123456789012345678",
            })

        monkeypatch.setattr(bot.httpx, "AsyncClient", _ClientFactory(handler))
        data = _run(bot._fetch_current("http://test", "project_a"))
        assert data["chat_model"] == "gpt-4o-mini"
        assert captured["url"].params["repo"] == "project_a"


# ---------------------------------------------------------------------------
# _set_model
# ---------------------------------------------------------------------------


class TestSetModel:
    def test_success_returns_200_and_body(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.method == "POST"
            return httpx.Response(200, json={"chat_model": "gpt-4o-mini"})

        monkeypatch.setattr(bot.httpx, "AsyncClient", _ClientFactory(handler))
        status, body = _run(bot._set_model("http://t", "r", "gpt-4o-mini", "u"))
        assert status == 200
        assert body == {"chat_model": "gpt-4o-mini"}

    def test_400_surfaces_error_and_available(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": "unknown model: x", "available": ["a", "b"]},
            )

        monkeypatch.setattr(bot.httpx, "AsyncClient", _ClientFactory(handler))
        status, body = _run(bot._set_model("http://t", "r", "x", "u"))
        assert status == 400
        assert body["error"].startswith("unknown model")
        assert body["available"] == ["a", "b"]

    def test_body_forwards_repo_model_and_user(self, monkeypatch):
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["body"] = _json.loads(req.content.decode())
            return httpx.Response(200, json={})

        monkeypatch.setattr(bot.httpx, "AsyncClient", _ClientFactory(handler))
        _run(bot._set_model("http://t", "proj", "mdl", "999"))
        assert captured["body"] == {
            "repo": "proj",
            "chat_model": "mdl",
            "updated_by": "999",
        }


# ---------------------------------------------------------------------------
# Permission gate (the decorated command closure is exercised via the same
# helper path; we test the permission check directly by simulating a fake
# interaction).
# ---------------------------------------------------------------------------


class _FakePerms:
    def __init__(self, manage_guild: bool) -> None:
        self.manage_guild = manage_guild


class _FakeUser:
    def __init__(self, manage_guild: bool, user_id: int = 12345) -> None:
        self.guild_permissions = _FakePerms(manage_guild)
        self.id = user_id


class _FakeResponse:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_message(self, content: str, **kw: Any) -> None:
        self.sent.append((content, kw))

    async def defer(self, **kw: Any) -> None:
        self.sent.append(("__defer__", kw))


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, content: str, **kw: Any) -> None:
        self.sent.append((content, kw))


class _FakeInteraction:
    def __init__(self, manage_guild: bool, guild_id: int = 1) -> None:
        self.user = _FakeUser(manage_guild)
        self.guild_id = guild_id
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class TestPermissionGate:
    def test_manage_guild_false_rejected(self, monkeypatch):
        monkeypatch.setattr(bot, "GUILD_ALLOWLIST", set())  # allow all guilds
        inter = _FakeInteraction(manage_guild=False)
        # Access the underlying callback — AppCommand wraps with a descriptor.
        cb = bot._model_set.callback
        _run(cb(inter, name="any"))
        assert len(inter.response.sent) == 1
        msg, kw = inter.response.sent[0]
        assert "Manage Server" in msg
        assert kw.get("ephemeral") is True

    def test_manage_guild_true_attempts_http(self, monkeypatch):
        monkeypatch.setattr(bot, "GUILD_ALLOWLIST", set())
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"chat_model": "gpt-4o-mini"})

        monkeypatch.setattr(bot.httpx, "AsyncClient", _ClientFactory(handler))
        inter = _FakeInteraction(manage_guild=True)
        cb = bot._model_set.callback
        _run(cb(inter, name="gpt-4o-mini"))
        # The handler made at least one request (the POST).
        assert calls["n"] >= 1
        # And responded with success.
        assert any("now" in msg for msg, _ in inter.followup.sent)
