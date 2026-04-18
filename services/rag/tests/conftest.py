"""Pytest conftest for the rag service tests.

`app.py` reads three required env vars at import time (`LITELLM_BASE_URL`,
`LITELLM_API_KEY`, `POSTGRES_DSN`) and constructs an OpenAI client plus a
FastAPI app instance. None of that issues network calls at import, so we
simply set dummy env vars here so imports succeed.

Real query-path tests (which touch Postgres + LiteLLM) live in the
integration suite (task 07); this service's unit tests are restricted to an
import sentinel that catches module-level breakage early.
"""

from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("LITELLM_BASE_URL", "http://litellm.local")
os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("POSTGRES_DSN", "postgresql://user:pw@localhost/db")

_SERVICE_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
