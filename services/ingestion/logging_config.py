"""Structured JSON logging for the ingestion service.

See services/rag/logging_config.py for the full docstring — this is an
identical copy, duplicated per service so each container image carries its
own copy and there's no shared-package build step. Keep the three
implementations in lockstep.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
from typing import Any

_STANDARD_LOGRECORD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: str | int | None = None) -> None:
    if level is None:
        level = os.environ.get("LOG_LEVEL", "INFO")

    root = logging.getLogger()
    root.setLevel(level)

    for h in root.handlers:
        if isinstance(h.formatter, JsonFormatter):
            return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
