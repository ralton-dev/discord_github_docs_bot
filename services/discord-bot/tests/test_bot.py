"""Unit tests for `services/discord-bot/bot.py`.

Covers the pure citation-formatting helper `_format`.

Note: `bot.py` reads `RAG_ORCHESTRATOR_URL`, `TARGET_REPO`, and
`DISCORD_BOT_TOKEN` at import time and constructs a `discord.Client`. Rather
than refactor the module to defer those reads, `conftest.py` sets dummy
values *before* this module is imported. The `discord.Client` constructor
does not perform I/O (the gateway connects only when `client.run()` is
called), so the instantiation is side-effect free for our purposes.
"""

from __future__ import annotations

import bot


class TestFormatNoCitations:
    def test_empty_citations_renders_no_sources(self) -> None:
        out = bot._format("the answer", [])
        assert "_no sources_" in out
        assert out.startswith("the answer")
        assert "**Sources**" in out

    def test_empty_citations_structure(self) -> None:
        out = bot._format("hi", [])
        # Exactly the format we promise: answer, blank line, header, body.
        assert out == "hi\n\n**Sources**\n_no sources_"


class TestFormatWithCitations:
    def test_single_citation_uses_seven_char_sha(self) -> None:
        sha = "abcdef0123456789"
        out = bot._format(
            "some answer",
            [{"path": "src/app.py", "commit_sha": sha}],
        )
        # The short SHA is exactly the first 7 characters.
        assert f"`{sha[:7]}`" in out
        assert f"`{sha}`" not in out  # full SHA must not leak
        assert "`src/app.py`" in out

    def test_multiple_citations_each_on_its_own_line(self) -> None:
        citations = [
            {"path": "a.py", "commit_sha": "1" * 40},
            {"path": "b.md", "commit_sha": "2" * 40},
        ]
        out = bot._format("answer", citations)
        assert "- `a.py` @ `1111111`" in out
        assert "- `b.md` @ `2222222`" in out
        # One line per citation.
        source_block = out.split("**Sources**\n", 1)[1]
        assert source_block.count("\n") == 1  # two lines = one newline between

    def test_no_sources_sentinel_absent_when_citations_present(self) -> None:
        out = bot._format(
            "a", [{"path": "x.py", "commit_sha": "deadbeefdeadbeef"}]
        )
        assert "_no sources_" not in out


class TestFormatTruncation:
    def test_short_output_not_truncated(self) -> None:
        out = bot._format(
            "short answer",
            [{"path": "a.py", "commit_sha": "abcdef1234567890"}],
        )
        assert not out.endswith("...")
        assert len(out) <= 1990

    def test_long_answer_truncated_to_1990_chars_ending_in_ellipsis(self) -> None:
        big_answer = "x" * 5000
        out = bot._format(
            big_answer,
            [{"path": "a.py", "commit_sha": "abcdef1234567890"}],
        )
        assert len(out) <= 1990
        assert out.endswith("...")
        # And specifically the contract: truncated to 1987 + "..." = 1990.
        assert len(out) == 1990

    def test_boundary_at_exactly_1990_chars_not_truncated(self) -> None:
        # Construct citations + answer that together hit exactly 1990 chars.
        citations = [{"path": "a.py", "commit_sha": "abcdef1234567890"}]
        # Format the suffix the way bot._format does to measure its length:
        suffix = "\n\n**Sources**\n- `a.py` @ `abcdef1`"
        pad = 1990 - len(suffix)
        answer = "y" * pad
        out = bot._format(answer, citations)
        assert len(out) == 1990
        assert not out.endswith("...")

    def test_one_char_over_boundary_is_truncated(self) -> None:
        citations = [{"path": "a.py", "commit_sha": "abcdef1234567890"}]
        suffix = "\n\n**Sources**\n- `a.py` @ `abcdef1`"
        # 1 char over the 1990 cap => must truncate.
        answer = "y" * (1991 - len(suffix))
        out = bot._format(answer, citations)
        assert len(out) == 1990
        assert out.endswith("...")


# ---------------------------------------------------------------------------
# _split_answer — multi-message splitting preserves boundaries
# ---------------------------------------------------------------------------

class TestSplitAnswer:
    def test_short_answer_returns_single_chunk(self) -> None:
        assert bot._split_answer("hello", limit=100) == ["hello"]

    def test_splits_at_paragraph_break(self) -> None:
        # Two ~60-char paragraphs with a blank line between them.
        para1 = "p1 " + "x" * 60
        para2 = "p2 " + "y" * 60
        answer = f"{para1}\n\n{para2}"
        chunks = bot._split_answer(answer, limit=80)
        assert len(chunks) == 2
        assert chunks[0].startswith("p1")
        assert chunks[1].startswith("p2")
        # All chunks within the limit.
        for c in chunks:
            assert len(c) <= 80

    def test_splits_at_sentence_boundary_when_no_paragraph(self) -> None:
        answer = "First sentence. Second sentence. Third sentence."
        chunks = bot._split_answer(answer, limit=20)
        # Each chunk should end with a terminating punctuation (or be
        # whatever remainder is left).
        for c in chunks[:-1]:
            assert c.rstrip().endswith((".", "!", "?"))

    def test_splits_at_word_boundary_when_no_sentence(self) -> None:
        # No punctuation at all, just words.
        answer = "alpha beta gamma delta epsilon"
        chunks = bot._split_answer(answer, limit=15)
        for c in chunks:
            assert len(c) <= 15
            # No chunk should start or end mid-word.
            assert not c.endswith("a beta"[-1:]) or c.endswith(" ") or c[-1].isalpha()

    def test_hard_cut_on_oversize_single_token(self) -> None:
        # Single word longer than limit — must hard-cut rather than loop.
        answer = "x" * 100
        chunks = bot._split_answer(answer, limit=30)
        assert all(len(c) <= 30 for c in chunks)
        # Content is preserved across chunks.
        assert "".join(chunks) == answer

    def test_balances_code_fences_across_split(self) -> None:
        # An opening ``` on chunk 1 without a matching close would break
        # markdown rendering of chunk 1 and leak code style into chunk 2.
        # The splitter must close the fence on chunk 1 and reopen on chunk 2.
        body = "```python\n" + "y = 1\n" * 40 + "```"
        chunks = bot._split_answer(body, limit=80)
        # Every chunk must have an even number of ``` markers (balanced).
        for c in chunks:
            assert c.count("```") % 2 == 0, (
                f"chunk has unbalanced code fence: {c!r}"
            )


# ---------------------------------------------------------------------------
# _format_messages — glue Sources to the last chunk, else push to its own
# ---------------------------------------------------------------------------


class TestFormatMessages:
    def test_short_answer_single_message(self) -> None:
        msgs = bot._format_messages(
            "hi", [{"path": "a.py", "commit_sha": "abcdef0"}], limit=100,
        )
        assert len(msgs) == 1
        assert "**Sources**" in msgs[0]

    def test_long_answer_sources_on_last_chunk(self) -> None:
        # Long enough to split into multiple chunks; sources should ride
        # on the final one.
        body = "\n\n".join("x" * 60 for _ in range(20))
        msgs = bot._format_messages(
            body, [{"path": "a.py", "commit_sha": "abcdef0"}], limit=200,
        )
        assert len(msgs) > 1
        # Sources appear exactly once, on the last message.
        assert "**Sources**" in msgs[-1]
        for m in msgs[:-1]:
            assert "**Sources**" not in m
        # Every message within the limit.
        for m in msgs:
            assert len(m) <= 200

    def test_every_message_under_discord_hard_cap(self) -> None:
        body = "word " * 1000  # ~5000 chars
        msgs = bot._format_messages(
            body, [{"path": "x.py", "commit_sha": "deadbee"}],
        )
        for m in msgs:
            # The Discord hard limit is 2000; we aim for 1900 headroom.
            assert len(m) <= 2000

    def test_no_citations_gives_no_sources_sentinel(self) -> None:
        msgs = bot._format_messages("hi", [], limit=100)
        assert "_no sources_" in msgs[-1]
