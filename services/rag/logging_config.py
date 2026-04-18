"""Structured JSON logging for the rag service.

Stdlib-only implementation — no `python-json-logger`, no `structlog`.

Each log record is emitted as a single JSON object with the keys:

    {
      "ts":     ISO-8601 timestamp in UTC (with 'Z' suffix),
      "level":  canonical level name ("INFO", "WARN", "ERROR", ...),
      "logger": record.name (e.g. "gitdoc.rag"),
      "msg":    formatted message (record.getMessage()),
      ...       any `extra={...}` kwargs merged in at the top level
    }

Exception info, when present, is emitted as `"exc": "<traceback>"`.

The entire configuration is one handler on the root logger (no propagation
back to uvicorn's handler, which would double-print). Idempotent: calling
`configure()` twice reuses the existing handler rather than stacking them.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
from typing import Any

# Keys the stdlib LogRecord carries by default. Anything the caller attaches
# via `logger.info("...", extra={"event": "foo", "repo": "bar"})` will NOT be
# in this set, and so will be merged into the JSON payload.
_STANDARD_LOGRECORD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Serialise each LogRecord to a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401 - stdlib override
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Merge any `extra={...}` attributes the caller attached. These live
        # directly on the record alongside the stdlib attrs; we filter by
        # excluding the known-stdlib keys.
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure(level: str | int | None = None) -> None:
    """Install the JSON handler on the root logger. Idempotent."""
    if level is None:
        level = os.environ.get("LOG_LEVEL", "INFO")

    root = logging.getLogger()
    root.setLevel(level)

    for h in root.handlers:
        if isinstance(h.formatter, JsonFormatter):
            return  # already configured

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    # Replace any default handlers (e.g. basicConfig's StreamHandler) so we
    # don't double-emit every record.
    root.handlers = [handler]
