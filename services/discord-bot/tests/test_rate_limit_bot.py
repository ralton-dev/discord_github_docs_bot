"""Tests for the bot-side rate-limit handling (plan 14).

Covers:

- ``_ask_orchestrator`` raises ``RateLimitedError`` on HTTP 429 — with the
  ``retry_after`` and ``reason`` decoded from the JSON body.
- ``_ask_orchestrator`` still falls back to single-turn on 422+history
  (pre-existing behaviour must be preserved).
- ``_ask_orchestrator`` forwards ``guild_id`` / ``user_id`` on the
  request body when supplied.
- ``_format_rate_limited_message`` produces the user-facing copy with
  the returned retry_after and reason.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Iterator

import httpx
import pytest

import bot


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def _capturing_handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    transport = httpx.MockTransport(_capturing_handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(bot.httpx, "AsyncClient", _factory)
    return captured


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestRateLimitedOn429:
    def test_429_raises_rate_limited_error(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={"error": "guild_budget", "retry_after": 123},
            )

        seen = _install_mock_transport(monkeypatch, handler)

        with pytest.raises(bot.RateLimitedError) as ei:
            _run(bot._ask_orchestrator("q?", guild_id="G1", user_id="U1"))

        assert ei.value.retry_after == 123
        assert ei.value.reason == "guild_budget"
        # Exactly one request — 429 must NOT retry silently.
        assert len(seen) == 1

    def test_429_user_budget_reason_propagates(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={"error": "user_budget", "retry_after": 7},
            )

        _install_mock_transport(monkeypatch, handler)
        with pytest.raises(bot.RateLimitedError) as ei:
            _run(bot._ask_orchestrator("q?", guild_id="G", user_id="U"))
        assert ei.value.reason == "user_budget"
        assert ei.value.retry_after == 7

    def test_429_missing_body_defaults_to_60s(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="no body")

        _install_mock_transport(monkeypatch, handler)
        with pytest.raises(bot.RateLimitedError) as ei:
            _run(bot._ask_orchestrator("q?", guild_id="G", user_id="U"))
        # Default fallback when the orchestrator returns a malformed body.
        assert ei.value.retry_after == 60


class TestPreservedHistoryFallback:
    """422+history behaviour must still work after the 429 path was added."""

    def test_422_with_history_retries_without_history(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            if "history" in body:
                return httpx.Response(422, json={"detail": "unknown field"})
            return httpx.Response(200, json={"answer": "ok", "citations": []})

        seen = _install_mock_transport(monkeypatch, handler)
        # Reset the latch so the behaviour is observable.
        bot._history_unsupported_logged = False
        out = _run(
            bot._ask_orchestrator(
                "q?",
                history=[{"role": "user", "content": "earlier"}],
                guild_id="G",
                user_id="U",
            )
        )
        assert out == {"answer": "ok", "citations": []}
        assert len(seen) == 2
        first = json.loads(seen[0].content)
        second = json.loads(seen[1].content)
        assert "history" in first
        assert "history" not in second
        # guild_id / user_id are forwarded on BOTH requests.
        assert first["guild_id"] == "G" and first["user_id"] == "U"
        assert second["guild_id"] == "G" and second["user_id"] == "U"


class TestIdentityForwarding:
    def test_guild_and_user_sent_when_provided(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answer": "ok", "citations": []})

        seen = _install_mock_transport(monkeypatch, handler)
        _run(bot._ask_orchestrator("q?", guild_id="42", user_id="99"))
        body = json.loads(seen[0].content)
        assert body["guild_id"] == "42"
        assert body["user_id"] == "99"

    def test_fields_omitted_when_none(self, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answer": "ok", "citations": []})

        seen = _install_mock_transport(monkeypatch, handler)
        _run(bot._ask_orchestrator("q?"))
        body = json.loads(seen[0].content)
        assert "guild_id" not in body
        assert "user_id" not in body


class TestFormattedMessage:
    def test_message_uses_retry_after_and_reason(self):
        err = bot.RateLimitedError(retry_after=321, reason="guild_budget")
        msg = bot._format_rate_limited_message(err)
        assert "321" in msg
        assert "guild_budget" in msg
        # Contract: user sees both the wait time and the budget name.
        assert "too fast" in msg.lower()

    def test_message_with_user_budget(self):
        err = bot.RateLimitedError(retry_after=5, reason="user_budget")
        msg = bot._format_rate_limited_message(err)
        assert "5 seconds" in msg
        assert "user_budget" in msg
