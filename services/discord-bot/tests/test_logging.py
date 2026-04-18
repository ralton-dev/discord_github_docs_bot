"""Shape test for structured JSON logging in the discord-bot service."""
from __future__ import annotations

import io
import json
import logging

from logging_config import JsonFormatter, configure


class TestJsonFormatterShape:
    def test_basic_record_has_required_keys(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="gitdoc.bot",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hi %s",
            args=("there",),
            exc_info=None,
        )
        out = json.loads(fmt.format(record))
        assert set(out.keys()) >= {"ts", "level", "logger", "msg"}
        assert out["level"] == "INFO"
        assert out["logger"] == "gitdoc.bot"
        assert out["msg"] == "hi there"
        assert out["ts"].endswith("Z")

    def test_extra_kwargs_merged(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="gitdoc.bot",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="q",
            args=None,
            exc_info=None,
        )
        record.event = "bot.ask"
        record.query_id = "abc-123"
        record.guild_id = 42
        record.repo = "demo"
        record.single = False
        out = json.loads(fmt.format(record))
        assert out["event"] == "bot.ask"
        assert out["query_id"] == "abc-123"
        assert out["guild_id"] == 42
        assert out["repo"] == "demo"
        assert out["single"] is False


class TestConfigureIdempotent:
    def test_configure_called_twice_does_not_stack_handlers(self):
        configure()
        configure()
        root = logging.getLogger()
        json_handlers = [
            h for h in root.handlers if isinstance(h.formatter, JsonFormatter)
        ]
        assert len(json_handlers) == 1


class TestEndToEndHandlerSwap:
    def test_emits_single_line_json(self):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("gitdoc.bot.test")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            logger.info(
                "ask",
                extra={"event": "bot.ask", "query_id": "q1", "guild_id": 1, "repo": "r"},
            )
        finally:
            logger.removeHandler(handler)
            logger.propagate = True
        line = buf.getvalue().strip()
        assert "\n" not in line
        data = json.loads(line)
        assert data["event"] == "bot.ask"
        assert data["query_id"] == "q1"
