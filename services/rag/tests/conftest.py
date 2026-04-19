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


# ---------------------------------------------------------------------------
# Auto-mock `_get_chat_model_for_repo` so every /ask dispatch test has a
# "model is set" baseline. Tests that specifically want to exercise the
# "model not set" 409 path (see test_settings.py::TestAskRequiresModel)
# override this by monkeypatching psycopg.connect to return a None row.
# ---------------------------------------------------------------------------
import pytest  # noqa: E402 — needs sys.path patched above first


@pytest.fixture(autouse=True)
def _default_chat_model(monkeypatch, request):
    """Default every /ask test to a valid chat model.

    Tests that set `pytestmark = pytest.mark.no_autoset_model` or add
    `@pytest.mark.no_autoset_model` opt out — useful for tests that want
    to exercise the real `_get_chat_model_for_repo` path (cache TTL
    checks, DB-error fallback, the 409 path, etc.).
    """
    if request.node.get_closest_marker("no_autoset_model"):
        return
    try:
        import app as _app
    except Exception:
        # Module may be mid-import in some subprocess-import tests.
        return
    monkeypatch.setattr(
        _app, "_get_chat_model_for_repo", lambda repo: "test-chat-model",
    )
