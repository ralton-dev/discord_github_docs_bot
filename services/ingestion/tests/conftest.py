"""Pytest conftest for the ingestion service tests.

`ingest.py` reads several required environment variables at import time
(REPO_URL, REPO_NAME, POSTGRES_DSN, LITELLM_BASE_URL, LITELLM_API_KEY) and
instantiates an OpenAI client. We set dummy values here *before* pytest imports
the test modules, so importing `ingest` from inside a test doesn't blow up.

The OpenAI client instantiated at module import does not issue any network
calls until an API method is invoked, so it is safe to construct with dummy
values. Tests only exercise pure helpers (`_auth_url`, `iter_chunks`,
`splitter_for`) — nothing that talks to the LLM or the database.
"""

from __future__ import annotations

import os
import pathlib
import sys

# Set required env vars before any test module imports `ingest`.
os.environ.setdefault("REPO_URL", "https://example.com/org/repo.git")
os.environ.setdefault("REPO_NAME", "test-repo")
os.environ.setdefault("POSTGRES_DSN", "postgresql://user:pw@localhost/db")
os.environ.setdefault("LITELLM_BASE_URL", "http://litellm.local")
os.environ.setdefault("LITELLM_API_KEY", "sk-test")

# Make the service root importable so tests can `import ingest`.
_SERVICE_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
