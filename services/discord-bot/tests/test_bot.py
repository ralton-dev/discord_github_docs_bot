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
