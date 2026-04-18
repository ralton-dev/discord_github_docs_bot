"""Integration-test fixtures.

Spins up two long-lived dependencies once per test session:

1. A `pgvector/pgvector:pg16` Postgres container with the canonical
   `db/init.sql` applied on startup.
2. An in-process FastAPI mock implementing the OpenAI `/v1/embeddings` and
   `/v1/chat/completions` shapes, run in a uvicorn thread on a random port.

Both services read their configuration from environment variables at *import
time*. The fixtures here set those env vars before any test imports
`ingest` or `app`, so we don't need to refactor either module.
"""

from __future__ import annotations

import contextlib
import pathlib
import shutil
import socket
import sys
import threading
import time
from typing import Iterator

import psycopg
import pytest
import uvicorn
from testcontainers.postgres import PostgresContainer

# ---------------------------------------------------------------------------
# Paths.
# ---------------------------------------------------------------------------

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
INIT_SQL = REPO_ROOT / "db" / "init.sql"
FIXTURE_REPO = REPO_ROOT / "tests" / "fixtures" / "repo"
INGESTION_SRC = REPO_ROOT / "services" / "ingestion"
RAG_SRC = REPO_ROOT / "services" / "rag"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return a TCP port currently free on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(url: str, timeout_s: float = 10.0) -> None:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status < 500:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}")


# ---------------------------------------------------------------------------
# Mock LiteLLM (in-process uvicorn).
# ---------------------------------------------------------------------------

class _UvicornInThread:
    """uvicorn.Server.run() in a daemon thread, with explicit lifecycle."""

    def __init__(self, app, host: str, port: int) -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.host = host
        self.port = port

    def start(self) -> None:
        self.thread.start()
        # Block until the server reports it's accepting connections — we
        # already poll over HTTP below, but waiting on `started` first makes
        # the failure mode clearer if uvicorn refuses to bind.
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("uvicorn failed to start within 5s")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="session")
def mock_litellm() -> Iterator[str]:
    """Start the mock OpenAI-compatible server; yield its base URL."""
    # Make tests/integration importable so we can reach mock_litellm.server.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.integration.mock_litellm.server import build_app

    port = _free_port()
    runner = _UvicornInThread(build_app(), host="127.0.0.1", port=port)
    runner.start()

    base_url = f"http://127.0.0.1:{port}"
    _wait_for_http(f"{base_url}/health", timeout_s=10.0)

    try:
        yield base_url
    finally:
        runner.stop()


# ---------------------------------------------------------------------------
# Postgres + pgvector testcontainer.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    """Boot pgvector/pgvector:pg16 and apply db/init.sql."""
    # PostgresContainer subclasses DockerContainer; we override the default
    # postgres image with pgvector's (it's API-compatible).
    container = PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="gitdoc",
        password="gitdoc",
        dbname="gitdoc",
    )
    container.start()
    try:
        # Wait for the server to accept connections via psycopg directly —
        # testcontainers' built-in wait sometimes returns before the server
        # is fully ready to accept SQL.
        dsn = _container_dsn(container)
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=2) as c:
                    c.execute("SELECT 1").fetchone()
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

        # Apply schema.
        sql = INIT_SQL.read_text(encoding="utf-8")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)

        yield container
    finally:
        with contextlib.suppress(Exception):
            container.stop()


def _container_dsn(container: PostgresContainer) -> str:
    """Build a libpq-style DSN that psycopg accepts.

    `container.get_connection_url()` returns a SQLAlchemy URL
    (`postgresql+psycopg2://...`); psycopg refuses the `+psycopg2` driver
    spec, so we rebuild the libpq form directly from the container's
    introspected host/port.
    """
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    user = container.username
    pw = container.password
    db = container.dbname
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


@pytest.fixture(scope="session")
def pg_dsn(pg_container) -> str:
    return _container_dsn(pg_container)


# ---------------------------------------------------------------------------
# Per-test DB cleanup.
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_db(pg_dsn: str) -> Iterator[str]:
    """Truncate chunks + ingest_runs before each test that opts in.

    Yields the DSN so tests can easily open their own connections.
    """
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE chunks, ingest_runs, rate_limit_usage RESTART IDENTITY"
        )
    yield pg_dsn


# ---------------------------------------------------------------------------
# Service env-var setup.
# ---------------------------------------------------------------------------

@pytest.fixture
def ingest_env(monkeypatch: pytest.MonkeyPatch, pg_dsn: str, mock_litellm: str):
    """Set the env vars `ingest.py` reads at import time, before importing it."""
    monkeypatch.setenv("REPO_URL", "file://" + str(FIXTURE_REPO))
    monkeypatch.setenv("REPO_NAME", "sentinel")
    monkeypatch.setenv("REPO_BRANCH", "main")
    monkeypatch.setenv("POSTGRES_DSN", pg_dsn)
    monkeypatch.setenv("LITELLM_BASE_URL", mock_litellm + "/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-mock")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBED_BATCH", "32")


@pytest.fixture
def rag_env(monkeypatch: pytest.MonkeyPatch, pg_dsn: str, mock_litellm: str):
    """Set the env vars `app.py` reads at import time, before importing it."""
    monkeypatch.setenv("POSTGRES_DSN", pg_dsn)
    monkeypatch.setenv("LITELLM_BASE_URL", mock_litellm + "/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-mock")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("CHAT_MODEL", "ollama_chat/llama3.2:3b")


# ---------------------------------------------------------------------------
# Module loaders that respect the per-test env (and avoid cross-test bleed).
# ---------------------------------------------------------------------------

def _load_module(name: str, src_dir: pathlib.Path):
    """Load a service module by adding its dir to sys.path and re-importing.

    `services/<svc>/tests/conftest.py` may already have inserted the dir at
    pytest collection time. We force a fresh import so the env vars set by
    the integration fixtures take effect.
    """
    sys.modules.pop(name, None)
    if str(src_dir) in sys.path:
        sys.path.remove(str(src_dir))
    sys.path.insert(0, str(src_dir))
    return __import__(name)


@pytest.fixture
def ingest_module(ingest_env, monkeypatch: pytest.MonkeyPatch):
    """Import the ingestion service with monkeypatched `clone` for fixture use."""
    mod = _load_module("ingest", INGESTION_SRC)

    def fake_clone(dest: pathlib.Path) -> str:
        # Skip git clone entirely — copy the fixture tree and synthesise a
        # commit SHA. The downstream code only uses the SHA as an opaque
        # string for upserts and GC.
        shutil.copytree(FIXTURE_REPO, dest, dirs_exist_ok=True)
        return "fixture-commit-" + "0" * 24  # 40-ish char placeholder

    monkeypatch.setattr(mod, "clone", fake_clone)
    return mod


@pytest.fixture
def rag_app(rag_env):
    """Import the rag FastAPI app fresh for the test."""
    return _load_module("app", RAG_SRC)
