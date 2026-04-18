"""Shape test for structured JSON logging in the rag service."""
from __future__ import annotations

import io
import json
import logging

from logging_config import JsonFormatter, configure


class TestJsonFormatterShape:
    def test_basic_record_has_required_keys(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="gitdoc.rag",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        out = json.loads(fmt.format(record))
        assert set(out.keys()) >= {"ts", "level", "logger", "msg"}
        assert out["level"] == "INFO"
        assert out["logger"] == "gitdoc.rag"
        assert out["msg"] == "hello world"
        # ISO-8601 with 'Z' (UTC) suffix.
        assert out["ts"].endswith("Z")

    def test_extra_kwargs_are_merged_into_payload(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="gitdoc.rag",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="m",
            args=None,
            exc_info=None,
        )
        # Simulate what logger.info(msg, extra={...}) does — attributes are
        # set directly on the record.
        record.event = "ask.received"
        record.repo = "example"
        record.query_chars = 42
        out = json.loads(fmt.format(record))
        assert out["event"] == "ask.received"
        assert out["repo"] == "example"
        assert out["query_chars"] == 42

    def test_exception_serialized_as_exc_field(self):
        fmt = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="gitdoc.rag",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="kaboom",
            args=None,
            exc_info=exc_info,
        )
        out = json.loads(fmt.format(record))
        assert "exc" in out
        assert "ValueError: boom" in out["exc"]

    def test_non_serializable_values_survive_via_default_str(self):
        """Handler callers occasionally pass non-JSON-native types."""
        fmt = JsonFormatter()

        class Weird:
            def __str__(self):
                return "<weird>"

        record = logging.LogRecord(
            name="gitdoc.rag",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="m",
            args=None,
            exc_info=None,
        )
        record.thing = Weird()
        out = json.loads(fmt.format(record))
        assert out["thing"] == "<weird>"


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
    """Drive a real logger through a captured stream to confirm shape."""

    def test_emits_single_line_json(self):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("gitdoc.rag.test")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            logger.info("line", extra={"event": "t", "n": 1})
        finally:
            logger.removeHandler(handler)
            logger.propagate = True
        line = buf.getvalue().strip()
        # No embedded newlines — one log line, one JSON object.
        assert "\n" not in line
        data = json.loads(line)
        assert data["event"] == "t"
        assert data["n"] == 1
