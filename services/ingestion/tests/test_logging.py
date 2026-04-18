"""Shape test for structured JSON logging in the ingestion service."""
from __future__ import annotations

import io
import json
import logging

from logging_config import JsonFormatter, configure


class TestJsonFormatterShape:
    def test_basic_record_has_required_keys(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="gitdoc.ingest",
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
        assert out["logger"] == "gitdoc.ingest"
        assert out["msg"] == "hello world"
        assert out["ts"].endswith("Z")

    def test_extra_event_fields_merge(self):
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="gitdoc.ingest",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="m",
            args=None,
            exc_info=None,
        )
        record.event = "ingest.batch"
        record.batch_size = 64
        record.total = 512
        out = json.loads(fmt.format(record))
        assert out["event"] == "ingest.batch"
        assert out["batch_size"] == 64
        assert out["total"] == 512


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
        logger = logging.getLogger("gitdoc.ingest.test")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            logger.info(
                "batched",
                extra={"event": "ingest.batch", "batch_size": 3, "total": 9},
            )
        finally:
            logger.removeHandler(handler)
            logger.propagate = True
        line = buf.getvalue().strip()
        assert "\n" not in line
        data = json.loads(line)
        assert data["event"] == "ingest.batch"
        assert data["batch_size"] == 3
