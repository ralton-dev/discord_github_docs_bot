import logging
import math
import os
import time
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from openai import OpenAI
from pgvector.psycopg import register_vector
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

import metrics
from logging_config import configure as configure_logging
from webhook import RateLimiter, SignatureError, verify_signature

configure_logging()
log = logging.getLogger("gitdoc.rag")

LITELLM_BASE = os.environ["LITELLM_BASE_URL"]
LITELLM_KEY  = os.environ["LITELLM_API_KEY"]
PG_DSN       = os.environ["POSTGRES_DSN"]
EMBED_MODEL  = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
CHAT_MODEL   = os.environ.get("CHAT_MODEL",  "claude-opus-4-7")

# Webhook settings. WEBHOOK_SECRET is optional at import time so the service
# still runs on instances that leave webhooks disabled (webhook.enabled=false
# in the chart); requests to /webhook will be rejected with 401 until it is
# set. The CronJob name is templated in by the chart — it is what we read
# `jobTemplate.spec` from to spawn ad-hoc Jobs.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
STALENESS_THRESHOLD_SECS = int(os.environ.get("STALENESS_THRESHOLD_SECS", "7200"))
# Default is the canonical chart name `gitdoc-<repo>-ingest`. The chart
# injects the exact value so multi-tenant renames don't need a code change.
INGEST_CRONJOB_NAME = os.environ.get("INGEST_CRONJOB_NAME", "")
NAMESPACE = os.environ.get("POD_NAMESPACE", "")

SYSTEM_PROMPT = """You are an assistant for a software project. Answer questions
using ONLY the provided context from the project's documentation and code.

Rules:
- If the answer is not in the context, say you don't know. Do not speculate.
- Quote file paths when you cite specific details.
- Be concise. Prefer short answers with code examples when relevant.
"""

llm = OpenAI(base_url=LITELLM_BASE, api_key=LITELLM_KEY)
app = FastAPI(title="gitdoc-rag")

# Module-level rate limiter — one token per repo per 60s. Shared across
# request handlers via dependency injection so tests can swap it out.
_rate_limiter = RateLimiter(interval_secs=60.0)

# Runtime model selection (plan 17).
#
# /models proxies LiteLLM's /v1/models and caches the result for 60s so
# autocomplete from the Discord bot doesn't hammer the backend. The per-repo
# chat_model lookup caches for 15s so /ask doesn't touch Postgres on every
# hot query. Both caches accept an injectable clock via the module-level
# _clock hook so tests can age the cache deterministically without sleeping.
_MODELS_TTL = 60.0
_SETTINGS_TTL = 15.0
_clock = time.monotonic
_models_cache: dict[str, Any] = {"data": [], "fetched_at": -1e18}
_settings_cache: dict[str, tuple[str | None, float]] = {}


def _fetch_models(force: bool = False) -> list[str]:
    """Return model IDs from LiteLLM, cached for _MODELS_TTL seconds."""
    now = _clock()
    if not force and (now - _models_cache["fetched_at"]) < _MODELS_TTL:
        return list(_models_cache["data"])
    resp = llm.models.list()
    ids = [m.id for m in resp.data]
    _models_cache["data"] = ids
    _models_cache["fetched_at"] = now
    return list(ids)


def _invalidate_models_cache() -> None:
    _models_cache["fetched_at"] = -1e18


def _get_chat_model_for_repo(repo: str) -> str:
    """Return the active chat model for ``repo``.

    Resolves to ``instance_settings.chat_model`` if set, otherwise the
    ``CHAT_MODEL`` env-var default. Cached per-repo for _SETTINGS_TTL so
    /ask does not touch Postgres on every request. The cache stores the
    resolved string (default or override) as a tuple ``(value, fetched_at)``.
    """
    now = _clock()
    cached = _settings_cache.get(repo)
    if cached is not None and (now - cached[1]) < _SETTINGS_TTL:
        return cached[0] or CHAT_MODEL
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True) as conn:
            row = conn.execute(
                "SELECT chat_model FROM instance_settings WHERE repo = %s",
                (repo,),
            ).fetchone()
    except Exception:
        log.exception("instance_settings lookup failed; falling back to env default")
        return CHAT_MODEL
    chat_model = row[0] if row and row[0] else None
    _settings_cache[repo] = (chat_model, now)
    return chat_model or CHAT_MODEL


def _invalidate_settings_cache(repo: str) -> None:
    _settings_cache.pop(repo, None)


class AskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    repo: str  = Field(min_length=1)
    top_k: int = Field(default=6, ge=1, le=20)


class Citation(BaseModel):
    path: str
    commit_sha: str


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation]


def _retrieve(repo: str, embedding: list[float], top_k: int):
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        register_vector(conn)
        return conn.execute(
            """
            SELECT path, commit_sha, content, content_type
            FROM chunks
            WHERE repo = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (repo, embedding, top_k),
        ).fetchall()


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    start = time.perf_counter()
    status = "error"
    hits = 0
    prompt_tokens = 0
    completion_tokens = 0
    # Resolve the active model per-request (plan 17). Cache means this is
    # cheap on the hot path; falls back to CHAT_MODEL when no override is set.
    model = _get_chat_model_for_repo(req.repo)
    log.info(
        "ask request received",
        extra={
            "event": "ask.received",
            "repo": req.repo,
            "query_chars": len(req.query),
        },
    )
    try:
        try:
            with metrics.timed(metrics.EMBED_LATENCY_SECONDS, req.repo):
                emb = (
                    llm.embeddings.create(model=EMBED_MODEL, input=req.query)
                    .data[0].embedding
                )
        except Exception as exc:
            log.exception("embedding call failed")
            raise HTTPException(
                status_code=502, detail="embedding backend unavailable"
            ) from exc

        rows = _retrieve(req.repo, emb, req.top_k)
        hits = len(rows)
        metrics.RETRIEVAL_HITS.labels(req.repo).observe(hits)

        if not rows:
            status = "empty"
            return AskResponse(
                answer=(
                    "I couldn't find anything relevant in the knowledge "
                    "base for that question."
                ),
                citations=[],
            )

        context_blocks = [
            f"## {path} ({ctype})\n{content}"
            for path, _sha, content, ctype in rows
        ]
        user_prompt = (
            "Context:\n\n"
            + "\n\n---\n\n".join(context_blocks)
            + f"\n\nQuestion: {req.query}"
        )

        try:
            with metrics.timed(metrics.CHAT_LATENCY_SECONDS, req.repo, model):
                completion = llm.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": user_prompt},
                    ],
                    temperature=0.1,
                    max_tokens=1024,
                )
        except Exception as exc:
            log.exception("chat call failed")
            raise HTTPException(
                status_code=502, detail="chat backend unavailable"
            ) from exc

        prompt_tokens, completion_tokens = metrics.record_chat_usage(
            completion, req.repo, model,
        )
        status = "ok"
        return AskResponse(
            answer=completion.choices[0].message.content or "",
            citations=[Citation(path=p, commit_sha=s) for p, s, _, _ in rows],
        )
    finally:
        elapsed = time.perf_counter() - start
        metrics.LATENCY_SECONDS.labels("ask").observe(elapsed)
        metrics.QUERIES_TOTAL.labels(req.repo, status).inc()
        log.info(
            "ask completed",
            extra={
                "event": "ask.completed",
                "repo": req.repo,
                "status": status,
                "latency_ms": int(elapsed * 1000),
                "hits": hits,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "model": model,
            },
        )


# ---------------------------------------------------------------------------
# Webhook ingestion
# ---------------------------------------------------------------------------


class _K8sDeps:
    """Thin wrapper the app uses to spawn Jobs; overridden in tests.

    The production path uses the official ``kubernetes`` Python client in
    in-cluster mode (ServiceAccount token under
    ``/var/run/secrets/kubernetes.io/serviceaccount``). Tests pass a fake
    implementation via :func:`get_k8s` to avoid touching a real cluster.
    """

    def __init__(self) -> None:
        # Lazy import so the module still imports fine on systems that do
        # not have the kubernetes client installed (e.g. when running a
        # subset of unit tests).
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            # Local-dev fallback — the kubeconfig lets integration tests
            # against a kind cluster share the same code path.
            config.load_kube_config()

        self._batch = client.BatchV1Api()
        self._client_module = client

    @property
    def batch(self):
        return self._batch

    @property
    def api(self):
        return self._client_module


def get_k8s() -> Any:
    """FastAPI dependency yielding the k8s client bundle.

    Default implementation talks to the real Kubernetes API. Tests override
    via ``app.dependency_overrides[get_k8s] = ...`` so handlers can be
    driven in-process without a cluster.
    """
    return _K8sDeps()


def get_rate_limiter() -> RateLimiter:
    """FastAPI dependency yielding the per-repo rate limiter."""
    return _rate_limiter


class WebhookResponse(BaseModel):
    queued: bool
    job: str | None = None
    reason: str | None = None
    retry_after: int | None = None


def _spawn_job_from_cronjob(k8s: Any, namespace: str, cronjob_name: str) -> str:
    """Spawn a Job from the CronJob's jobTemplate and return the new name.

    Implements the Python equivalent of ``kubectl create job --from=cronjob``:
    read the CronJob, copy ``spec.jobTemplate.spec`` into a fresh Job
    manifest with a unique name and owner-less metadata, and POST it.
    """
    # Read the CronJob so we can copy its jobTemplate.spec verbatim.
    cj = k8s.batch.read_namespaced_cron_job(name=cronjob_name, namespace=namespace)
    job_spec = cj.spec.job_template.spec
    job_name = f"{cronjob_name}-webhook-{int(time.time())}"

    job = k8s.api.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s.api.V1ObjectMeta(
            name=job_name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/component": "ingestion",
                "gitdoc.trigger": "webhook",
            },
            annotations={
                "gitdoc.ingestion/source": "webhook",
                "cronjob.kubernetes.io/instantiate": "manual",
            },
        ),
        spec=job_spec,
    )
    k8s.batch.create_namespaced_job(namespace=namespace, body=job)
    return job_name


@app.post("/webhook")
async def webhook(
    request: Request,
    k8s: Any = Depends(get_k8s),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    """Receive a GitHub/GitLab push webhook and spawn an ingestion Job.

    Response shape:

    - 401 ``{"error": "..."}`` on signature failure.
    - 429 ``{"queued": false, "reason": "rate-limited", "retry_after": N}``
      when the per-repo bucket is empty.
    - 202 ``{"queued": true, "job": "<name>"}`` on success.
    - 500 ``{"error": "..."}`` if the k8s API rejects the Job create.
    """
    body = await request.body()

    # Provider: GitHub sends no explicit provider field; we detect via
    # headers. The ``provider`` query/body field lets integrators be
    # explicit, but we auto-detect as a fallback so a bare webhook config
    # doesn't need custom payload shaping.
    github_sig = request.headers.get("X-Hub-Signature-256")
    gitlab_token = request.headers.get("X-Gitlab-Token")
    if github_sig:
        provider = "github"
        sig_header: str | None = github_sig
    elif gitlab_token:
        provider = "gitlab"
        sig_header = gitlab_token
    else:
        return JSONResponse(
            status_code=401,
            content={"error": "missing signature header"},
        )

    try:
        verify_signature(
            provider=provider,
            secret=WEBHOOK_SECRET,
            body=body,
            signature_header=sig_header,
        )
    except SignatureError as exc:
        log.warning("webhook signature rejected: %s", exc)
        return JSONResponse(
            status_code=401,
            content={"error": str(exc)},
        )

    # Per-repo rate-limit — we key on the configured repo for this pod,
    # not the payload's repo. A chart instance is one repo, so this
    # effectively caps one ingestion every 60s per instance regardless
    # of payload spoofing.
    repo_key = os.environ.get("REPO_NAME") or os.environ.get("TARGET_REPO", "default")
    allowed, retry_after = rate_limiter.check(repo_key)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "queued": False,
                "reason": "rate-limited",
                "retry_after": int(math.ceil(retry_after)),
            },
        )

    if not INGEST_CRONJOB_NAME or not NAMESPACE:
        log.error(
            "webhook misconfigured: INGEST_CRONJOB_NAME=%r, POD_NAMESPACE=%r",
            INGEST_CRONJOB_NAME, NAMESPACE,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "webhook misconfigured (missing cronjob/namespace)"},
        )

    try:
        job_name = _spawn_job_from_cronjob(k8s, NAMESPACE, INGEST_CRONJOB_NAME)
    except Exception as exc:
        log.exception("failed to spawn ingestion job from webhook")
        return JSONResponse(
            status_code=500,
            content={"error": f"k8s create_namespaced_job failed: {exc}"},
        )

    return JSONResponse(
        status_code=202,
        content={"queued": True, "job": job_name},
    )


# ---------------------------------------------------------------------------
# Ingestion staleness status
# ---------------------------------------------------------------------------


class IngestionStatus(BaseModel):
    last_success_at: str | None
    seconds_since_last_success: int | None
    status: str  # "ok" | "stale" | "unknown"


@app.get("/status/ingestion", response_model=IngestionStatus)
def ingestion_status(repo: str):
    """Report the freshness of the last successful ingestion for ``repo``.

    - ``status="ok"``: a successful run exists within ``STALENESS_THRESHOLD_SECS``.
    - ``status="stale"``: successful run exists but older than the threshold.
    - ``status="unknown"``: no successful run recorded yet.
    """
    if not repo:
        raise HTTPException(status_code=400, detail="repo query param is required")

    try:
        with psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True) as conn:
            row = conn.execute(
                """
                SELECT finished_at
                FROM ingest_runs
                WHERE repo = %s AND status = 'ok' AND finished_at IS NOT NULL
                ORDER BY finished_at DESC
                LIMIT 1
                """,
                (repo,),
            ).fetchone()
    except Exception as exc:
        log.exception("status/ingestion db query failed")
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}") from exc

    if row is None:
        return IngestionStatus(
            last_success_at=None,
            seconds_since_last_success=None,
            status="unknown",
        )

    finished_at = row[0]
    import datetime as _dt  # local import to keep module import cheap

    now = _dt.datetime.now(tz=finished_at.tzinfo or _dt.timezone.utc)
    delta = int((now - finished_at).total_seconds())
    status = "ok" if delta < STALENESS_THRESHOLD_SECS else "stale"
    return IngestionStatus(
        last_success_at=finished_at.isoformat(),
        seconds_since_last_success=delta,
        status=status,
    )


# ---------------------------------------------------------------------------
# Model selection (plan 17)
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    id: str


class ModelsResponse(BaseModel):
    data: list[ModelInfo]


class SettingsResponse(BaseModel):
    repo: str
    chat_model: str | None
    updated_at: str | None
    updated_by: str | None


class SettingsUpdate(BaseModel):
    repo: str = Field(min_length=1)
    chat_model: str = Field(min_length=1)
    updated_by: str | None = None


@app.get("/models", response_model=ModelsResponse)
def list_models():
    """Proxy LiteLLM's /v1/models with a 60s in-process cache.

    Returned shape matches OpenAI's: ``{"data": [{"id": "..."}, ...]}`` so the
    Discord bot and any external tooling can reuse the same parser.
    """
    try:
        ids = _fetch_models()
    except Exception as exc:
        log.exception("LiteLLM /v1/models proxy failed")
        raise HTTPException(status_code=502, detail=f"models backend unavailable: {exc}") from exc
    return ModelsResponse(data=[ModelInfo(id=i) for i in ids])


@app.get("/settings", response_model=SettingsResponse)
def get_settings(repo: str):
    """Return the persisted instance settings for ``repo``.

    Absent row → ``chat_model: null`` (not 404), so the bot can render
    "using default" without branching on error shape.
    """
    if not repo:
        raise HTTPException(status_code=400, detail="repo query param is required")
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True) as conn:
            row = conn.execute(
                """
                SELECT chat_model, updated_at, updated_by
                FROM instance_settings
                WHERE repo = %s
                """,
                (repo,),
            ).fetchone()
    except Exception as exc:
        log.exception("instance_settings GET failed")
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}") from exc
    if row is None:
        return SettingsResponse(
            repo=repo, chat_model=None, updated_at=None, updated_by=None,
        )
    chat_model, updated_at, updated_by = row
    return SettingsResponse(
        repo=repo,
        chat_model=chat_model,
        updated_at=updated_at.isoformat() if updated_at else None,
        updated_by=updated_by,
    )


@app.post("/settings", response_model=SettingsResponse)
def update_settings(body: SettingsUpdate):
    """Upsert the chat model for ``body.repo``.

    Validates ``body.chat_model`` against the cached /v1/models list; 400
    with the full available list when the name is unknown, so the client
    can render an actionable error.
    """
    try:
        available = _fetch_models()
    except Exception as exc:
        log.exception("could not fetch models for validation")
        raise HTTPException(status_code=502, detail=f"models backend unavailable: {exc}") from exc
    if body.chat_model not in available:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"unknown model: {body.chat_model}",
                "available": available,
            },
        )
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True) as conn:
            row = conn.execute(
                """
                INSERT INTO instance_settings (repo, chat_model, updated_at, updated_by)
                VALUES (%s, %s, now(), %s)
                ON CONFLICT (repo) DO UPDATE
                SET chat_model = EXCLUDED.chat_model,
                    updated_at = now(),
                    updated_by = EXCLUDED.updated_by
                RETURNING chat_model, updated_at, updated_by
                """,
                (body.repo, body.chat_model, body.updated_by),
            ).fetchone()
    except Exception as exc:
        log.exception("instance_settings POST failed")
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}") from exc
    # Hot path wins — drop any cached value so the next /ask picks this up.
    _invalidate_settings_cache(body.repo)
    chat_model, updated_at, updated_by = row
    return SettingsResponse(
        repo=body.repo,
        chat_model=chat_model,
        updated_at=updated_at.isoformat() if updated_at else None,
        updated_by=updated_by,
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}") from exc
    return {"ok": True}


@app.get("/metrics")
def prometheus_metrics():
    """Expose Prometheus metrics from the default global registry.

    Returns the standard text exposition format that Prometheus scrapers
    (including the Prometheus Operator's ServiceMonitor) expect. See
    `services/rag/metrics.py` for the list of metrics and their labels.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ---------------------------------------------------------------------------
# Error handlers — JSON, not Pydantic's default HTML-ish body.
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    # Pydantic validation errors default to 422 with a structured JSON body.
    # FastAPI already returns JSON here; this override just ensures the
    # shape is always `{"error": ..., "details": ...}` across every route
    # — including /webhook — so integrators can parse uniformly.
    return JSONResponse(
        status_code=422,
        content={"error": "validation failed", "details": exc.errors()},
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )
