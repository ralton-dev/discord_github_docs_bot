"""Unit tests for the thread-aware conversation features in `bot.py`.

Covers:
- `_compact_citations` — the helper that shrinks prior bot answers'
  `**Sources**` blocks to a one-line `[src: ...]` summary.
- `_collect_thread_history` — async helper that walks a Discord thread
  and produces a `[{"role", "content"}, ...]` list, dropping oldest
  entries until the char budget is met.
- `_ask_orchestrator` — async helper that POSTs to the RAG service and
  gracefully degrades when the orchestrator returns 422 because it
  doesn't yet understand the `history` field.

We deliberately avoid `respx` here so the discord-bot service stays at
two runtime/test dependencies (discord.py + httpx). The tests stub the
orchestrator with `httpx.MockTransport`, which is part of httpx itself.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import contextmanager
from typing import Iterator

import httpx
import pytest

import bot


# --------------------------------------------------------------------------
# _compact_citations
# --------------------------------------------------------------------------


class TestCompactCitations:
    def test_no_sources_block_returns_empty(self) -> None:
        # If the prior message somehow doesn't contain `**Sources**`, we
        # should return an empty string rather than guessing.
        assert bot._compact_citations("just an answer with no header") == ""
        assert bot._compact_citations("") == ""

    def test_no_sources_sentinel_compacts_to_none_marker(self) -> None:
        block = bot._format("the answer", [])
        # Sanity: the formatter actually writes the sentinel.
        assert "_no sources_" in block
        assert bot._compact_citations(block) == "[src: none]"

    def test_single_source_compacts(self) -> None:
        block = bot._format(
            "an answer",
            [{"path": "src/app.py", "commit_sha": "abcdef0123456789"}],
        )
        assert bot._compact_citations(block) == "[src: src/app.py]"

    def test_multiple_sources_preserves_order_and_dedupes(self) -> None:
        block = bot._format(
            "an answer",
            [
                {"path": "a.py", "commit_sha": "1" * 40},
                {"path": "b.md", "commit_sha": "2" * 40},
                # Same path shows up at a different commit — should dedupe.
                {"path": "a.py", "commit_sha": "3" * 40},
                {"path": "c.py", "commit_sha": "4" * 40},
            ],
        )
        assert bot._compact_citations(block) == "[src: a.py, b.md, c.py]"

    def test_garbage_after_sources_header_returns_none_marker(self) -> None:
        # Sources header but unparseable content should still degrade
        # safely instead of raising or leaking the garbage downstream.
        text = "answer\n\n**Sources**\nthis is not a citation bullet"
        assert bot._compact_citations(text) == "[src: none]"


# --------------------------------------------------------------------------
# _collect_thread_history
# --------------------------------------------------------------------------


class _FakeUser:
    """Stand-in for a discord.User/Member with the `bot` attribute."""

    def __init__(self, *, is_bot: bool = False, id_: int = 42) -> None:
        self.bot = is_bot
        self.id = id_

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeUser) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


class _FakeMessage:
    def __init__(self, author: _FakeUser, content: str) -> None:
        self.author = author
        self.content = content


class _FakeThread:
    """Minimal Thread stand-in supporting `history(limit, oldest_first)`.

    discord.py yields newest-first by default; our helper passes
    `oldest_first=False`, so we honour that and yield messages in
    reverse-chronological order from a chronologically-stored list.
    """

    def __init__(self, messages: list[_FakeMessage]) -> None:
        # Stored in chronological order (oldest first).
        self._messages = messages

    def history(self, *, limit: int, oldest_first: bool = False):
        msgs = list(self._messages)
        if not oldest_first:
            msgs.reverse()
        msgs = msgs[:limit]

        async def _gen():
            for m in msgs:
                yield m

        return _gen()


@contextmanager
def _patch_client_user(monkeypatch: pytest.MonkeyPatch, bot_user: _FakeUser) -> Iterator[None]:
    """Pretend `bot.client.user` is `bot_user` for the test's duration.

    `discord.Client.user` is a property, so we monkeypatch the property
    at the class level. `monkeypatch` undoes the change at teardown, so
    the override does not leak between tests.
    """
    monkeypatch.setattr(type(bot.client), "user", property(lambda self: bot_user))
    yield


def _run(coro):
    """Tiny sync runner so we don't need pytest-asyncio."""
    return asyncio.new_event_loop().run_until_complete(coro)


class TestCollectThreadHistory:
    def test_alternating_user_and_assistant_in_chronological_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bot_user = _FakeUser(is_bot=True, id_=1)
        human = _FakeUser(is_bot=False, id_=2)
        # Chronological: assistant answers, then user, then assistant.
        # The assistant messages are previously-formatted bot answers (so
        # their `**Sources**` blocks should be compacted by the helper).
        first_assistant = bot._format(
            "first answer",
            [{"path": "a.py", "commit_sha": "1" * 40}],
        )
        second_assistant = bot._format(
            "second answer",
            [{"path": "b.py", "commit_sha": "2" * 40}],
        )
        thread = _FakeThread(
            [
                _FakeMessage(bot_user, first_assistant),
                _FakeMessage(human, "user follow-up?"),
                _FakeMessage(bot_user, second_assistant),
            ]
        )

        with _patch_client_user(monkeypatch, bot_user):
            out = _run(bot._collect_thread_history(thread, limit=10, char_budget=10_000))

        assert [e["role"] for e in out] == ["assistant", "user", "assistant"]
        # Assistant entries have been compacted, not the raw `_format` output.
        assert out[0]["content"] == "[src: a.py]"
        assert out[1]["content"] == "user follow-up?"
        assert out[2]["content"] == "[src: b.py]"

    def test_drops_oldest_until_under_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot_user = _FakeUser(is_bot=True, id_=1)
        human = _FakeUser(is_bot=False, id_=2)
        # Five user messages, each 100 chars. Budget = 250 -> we should keep
        # the last 2 (sum=200) since 3 would be 300 > 250.
        msgs = [_FakeMessage(human, "x" * 100) for _ in range(5)]
        thread = _FakeThread(msgs)

        with _patch_client_user(monkeypatch, bot_user):
            out = _run(bot._collect_thread_history(thread, limit=10, char_budget=250))

        assert len(out) == 2
        assert all(e["content"] == "x" * 100 for e in out)

    def test_char_budget_exactly_hit_keeps_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot_user = _FakeUser(is_bot=True, id_=1)
        human = _FakeUser(is_bot=False, id_=2)
        # Two messages of 50 chars each = exactly 100. Budget 100 must keep both.
        msgs = [_FakeMessage(human, "a" * 50), _FakeMessage(human, "b" * 50)]
        thread = _FakeThread(msgs)

        with _patch_client_user(monkeypatch, bot_user):
            out = _run(bot._collect_thread_history(thread, limit=10, char_budget=100))

        assert len(out) == 2

    def test_no_history_when_thread_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot_user = _FakeUser(is_bot=True, id_=1)
        thread = _FakeThread([])

        with _patch_client_user(monkeypatch, bot_user):
            out = _run(bot._collect_thread_history(thread, limit=10, char_budget=3000))

        assert out == []

    def test_skips_other_bots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot_user = _FakeUser(is_bot=True, id_=1)
        other_bot = _FakeUser(is_bot=True, id_=99)  # different bot id
        human = _FakeUser(is_bot=False, id_=2)
        thread = _FakeThread(
            [
                _FakeMessage(other_bot, "noise from another bot"),
                _FakeMessage(human, "actual question"),
            ]
        )
        with _patch_client_user(monkeypatch, bot_user):
            out = _run(bot._collect_thread_history(thread, limit=10, char_budget=3000))

        # Other bots are not included; only the human message survives.
        assert out == [{"role": "user", "content": "actual question"}]

    def test_oversize_single_message_is_still_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Edge case: even if one message blows the budget on its own,
        # we keep returning *something* rather than empty (the caller and
        # the orchestrator can truncate further).
        bot_user = _FakeUser(is_bot=True, id_=1)
        human = _FakeUser(is_bot=False, id_=2)
        thread = _FakeThread([_FakeMessage(human, "z" * 5000)])
        with _patch_client_user(monkeypatch, bot_user):
            out = _run(bot._collect_thread_history(thread, limit=10, char_budget=100))
        assert len(out) == 1


# --------------------------------------------------------------------------
# _ask_orchestrator
# --------------------------------------------------------------------------


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Replace `httpx.AsyncClient` with a fixed-transport stub.

    Returns a list that the test can inspect to see every request the
    bot fired. We monkeypatch the symbol the bot module imported, not
    the global `httpx.AsyncClient`, so we don't accidentally affect
    other tests in the session.
    """
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


class TestAskOrchestrator:
    def test_history_is_forwarded_when_supported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "answer": "the answer",
                    "citations": [{"path": "x.py", "commit_sha": "deadbeef0000000"}],
                },
            )

        seen = _install_mock_transport(monkeypatch, handler)

        history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
        ]
        out = _run(bot._ask_orchestrator("follow-up?", history=history))

        assert out["answer"] == "the answer"
        assert out["citations"][0]["path"] == "x.py"
        assert len(seen) == 1
        body = json.loads(seen[0].content)
        assert body["query"] == "follow-up?"
        assert body["repo"] == bot.REPO
        assert body["history"] == history

    def test_no_history_omits_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answer": "ok", "citations": []})

        seen = _install_mock_transport(monkeypatch, handler)
        _run(bot._ask_orchestrator("just one question"))

        assert len(seen) == 1
        body = json.loads(seen[0].content)
        assert "history" not in body

    def test_422_with_history_falls_back_to_single_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            if "history" in body:
                # Orchestrator hasn't shipped the history field yet.
                return httpx.Response(422, json={"detail": "unknown field history"})
            return httpx.Response(200, json={"answer": "fallback ok", "citations": []})

        seen = _install_mock_transport(monkeypatch, handler)

        # Reset the latch so we can observe the warning behaviour.
        bot._history_unsupported_logged = False

        out = _run(
            bot._ask_orchestrator(
                "q?",
                history=[{"role": "user", "content": "earlier"}],
            )
        )

        assert out == {"answer": "fallback ok", "citations": []}
        # First request had history, second (retry) did not.
        assert len(seen) == 2
        first = json.loads(seen[0].content)
        second = json.loads(seen[1].content)
        assert "history" in first
        assert "history" not in second
        # And the latch flipped so subsequent fallbacks won't re-log.
        assert bot._history_unsupported_logged is True

    def test_422_without_history_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # If the orchestrator returns 422 without us sending history, that
        # is a real validation error — surface it so callers don't silently
        # swallow it.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"detail": "query missing"})

        _install_mock_transport(monkeypatch, handler)

        with pytest.raises(httpx.HTTPStatusError):
            _run(bot._ask_orchestrator("q?"))
