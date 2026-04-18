import asyncio
import hashlib
import json
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
import reranker as reranker_mod
from logging_config import configure as configure_logging
from webhook import RateLimiter, SignatureError, verify_signature

configure_logging()
log = logging.getLogger("gitdoc.rag")

LITELLM_BASE = os.environ["LITELLM_BASE_URL"]
LITELLM_KEY  = os.environ["LITELLM_API_KEY"]
PG_DSN       = os.environ["POSTGRES_DSN"]
EMBED_MODEL  = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
# Chat model is NOT read from env. Plan 17 stores the active model in
# `instance_settings` and requires an explicit `/model set` before /ask
# will answer. A stale env-var default would mask misconfiguration.

# Hybrid retrieval (plan 11). When enabled, /ask blends pgvector similarity
# with a BM25-style ranking over the `chunks.content_tsv` GIN index using
# Reciprocal Rank Fusion. Set to "false" via the chart's
# `search.hybrid.enabled` to fall back to vector-only.
HYBRID_SEARCH_ENABLED = os.environ.get("HYBRID_SEARCH_ENABLED", "true").lower() == "true"
# RRF constant. 60 is the canonical value from the original RRF paper
# (Cormack et al., 2009) and is widely used as a safe default. Higher k
# flattens the contribution of top-of-list ranks, lower k makes top ranks
# dominate. Tune if recall on a specific corpus looks off.
RRF_K = 60

# Cross-encoder reranker (plan 12). When both the feature flag and the URL
# are set, ``/ask`` widens retrieval to ``top_k * RERANKER_MULT`` candidates,
# scores them with a cross-encoder, and keeps the top ``top_k``. Disabled
# by default — operators opt in via the chart's ``reranker.enabled`` once a
# reranker endpoint is provisioned (typically on the homelab Ollama).
RERANKER_ENABLED = os.environ.get("RERANKER_ENABLED", "false").lower() == "true"
RERANKER_URL = os.environ.get("RERANKER_URL", "")
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_MULT = int(os.environ.get("RERANKER_MULT", "3"))

# Query cache (plan 13). Short-circuits /ask when the same normalized
# query has already been answered against the repo's current commit_sha.
# Disable to force every request through retrieval + LLM (useful for
# benchmarking cold-cache latency or isolating a stale-answer bug).
QUERY_CACHE_ENABLED = os.environ.get("QUERY_CACHE_ENABLED", "true").lower() == "true"

# Rate limits & cost caps (plan 14). When enabled, every /ask request
# carrying (guild_id, user_id) is gated at the TOP of the handler — BEFORE
# the query cache — by summing `tokens` in `rate_limit_usage` for the last
# hour. Over budget = 429. Requests without both guild_id and user_id
# (e.g. curl probes, health-check tooling) bypass the gate unconditionally.
# Cache hits record tokens=0 so cached answers don't count against the
# budget; LLM completions record prompt + completion tokens from the
# OpenAI response.
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() == "true"
GUILD_TOKENS_PER_HOUR = int(os.environ.get("GUILD_TOKENS_PER_HOUR", "200000"))
USER_TOKENS_PER_HOUR = int(os.environ.get("USER_TOKENS_PER_HOUR", "50000"))

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


def _get_chat_model_for_repo(repo: str) -> str | None:
    """Return the DB-configured chat model for ``repo``, or ``None`` if unset.

    No env-var fallback. The operator must pick a model that LiteLLM
    actually exposes — which we can only verify against /v1/models at
    `/model set` time. Silently routing traffic to a stale env default
    would hide the misconfiguration; returning None surfaces it so
    callers can tell the user to run /model set.

    Cached per-repo for _SETTINGS_TTL so /ask doesn't touch Postgres on
    the hot path. DB errors also surface as None — we'd rather block
    /ask with a clear "not set" than keep answering from a stale guess.
    """
    now = _clock()
    cached = _settings_cache.get(repo)
    if cached is not None and (now - cached[1]) < _SETTINGS_TTL:
        return cached[0]
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True) as conn:
            row = conn.execute(
                "SELECT chat_model FROM instance_settings WHERE repo = %s",
                (repo,),
            ).fetchone()
    except Exception:
        log.exception("instance_settings lookup failed")
        return None
    chat_model = row[0] if row and row[0] else None
    _settings_cache[repo] = (chat_model, now)
    return chat_model


def _invalidate_settings_cache(repo: str) -> None:
    _settings_cache.pop(repo, None)


# ---------------------------------------------------------------------------
# Query cache helpers (plan 13).
#
# Normalisation: lowercase + collapse whitespace. "What is X?" and
# "  what is  x? " hash to the same bucket. We deliberately KEEP the
# trailing "?" and any other punctuation — two questions that differ
# only in phrasing (punctuation, casing, whitespace) should hit the same
# cache row, but semantically different ones must not.
#
# The commit_sha key is a per-repo 15s-cached lookup against ingest_runs
# so /ask does not touch Postgres just to resolve it on every hot
# request. A fresh ingestion invalidates the cache on the ingestion side
# (ingest.py GC step); the 15s TTL here is just "how quickly does a
# brand-new ingestion become visible to the cache layer" — the cached
# answers for the new SHA are keyed on it naturally, so stale reads
# during the TTL window don't surface stale content, just cause a brief
# cache-miss stretch after an ingest completes.
# ---------------------------------------------------------------------------


_COMMIT_TTL = 15.0
_commit_cache: dict[str, tuple[str | None, float]] = {}


def _normalize_query(q: str) -> str:
    """Lowercase + collapse whitespace for deterministic hashing.

    Intentionally preserves punctuation — two questions that differ
    only in case or whitespace should hit the same cache row; ones that
    differ in meaning must not.
    """
    return " ".join(q.lower().split())


def _query_hash(q: str) -> str:
    """SHA-256 of the normalized query string. Deterministic, 64 hex chars."""
    return hashlib.sha256(_normalize_query(q).encode("utf-8")).hexdigest()


def _latest_commit_sha(repo: str) -> str | None:
    """Return the most recent successful ``commit_sha`` for ``repo``.

    Cached per-repo for ``_COMMIT_TTL`` so the /ask hot path does not
    hit Postgres twice (once here, once for the cache lookup) on every
    request. None is returned when no successful ingestion exists yet —
    callers should skip cache lookup in that case, because there is
    nothing that could have populated the cache anyway.

    DB errors are swallowed and treated as "no SHA available" so a
    flaky DB degrades gracefully to "cache disabled for this request"
    without taking down /ask.
    """
    now = _clock()
    cached = _commit_cache.get(repo)
    if cached is not None and (now - cached[1]) < _COMMIT_TTL:
        return cached[0]
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True) as conn:
            row = conn.execute(
                """
                SELECT commit_sha
                FROM ingest_runs
                WHERE repo = %s AND status = 'ok'
                ORDER BY finished_at DESC NULLS LAST, started_at DESC
                LIMIT 1
                """,
                (repo,),
            ).fetchone()
    except Exception:
        log.exception("latest_commit_sha lookup failed")
        return None
    sha = row[0] if row else None
    _commit_cache[repo] = (sha, now)
    return sha


def _invalidate_commit_cache(repo: str | None = None) -> None:
    """Drop cached commit SHAs. Pass None to clear every repo."""
    if repo is None:
        _commit_cache.clear()
    else:
        _commit_cache.pop(repo, None)


class AskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    repo: str  = Field(min_length=1)
    top_k: int = Field(default=6, ge=1, le=20)
    # Rate-limit identity (plan 14). Optional so callers without a Discord
    # identity (curl probes, health-check flows) can still use /ask; the
    # rate-limit gate skips entirely when both are None. The Discord bot
    # always sends these on every /ask call.
    guild_id: str | None = Field(default=None)
    user_id: str | None = Field(default=None)


class Citation(BaseModel):
    path: str
    commit_sha: str


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation]


def _retrieve_vector(repo: str, embedding: list[float], top_k: int):
    """Vector-only retrieval — pure pgvector cosine similarity.

    Returned row shape: ``(path, commit_sha, content, content_type)``. Kept
    as the fallback path when ``HYBRID_SEARCH_ENABLED`` is false or when a
    future caller wants to bypass BM25 entirely.
    """
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


def _rrf_fuse(
    rankings: list[list[Any]],
    k: int = RRF_K,
) -> list[tuple[Any, float]]:
    """Pure Reciprocal Rank Fusion.

    Each input list is an ordered ranking of arbitrary hashable IDs (best
    first). For each ID, its RRF score is ``sum(1 / (k + rank))`` across
    every list it appears in (rank is 1-based). Returns ``(id, score)``
    tuples sorted by descending score; ties broken by first appearance
    across the input rankings (Python's sort is stable).

    Pure function — no DB, no I/O. Tested standalone in
    ``tests/test_hybrid.py`` so the fusion math has its own coverage
    independent of the SQL plumbing.
    """
    scores: dict[Any, float] = {}
    first_seen: dict[Any, int] = {}
    counter = 0
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            if item not in first_seen:
                first_seen[item] = counter
                counter += 1
    # Sort by (-score, first_seen) so ties are deterministic and stable.
    return sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))


def _retrieve_hybrid(
    repo: str,
    query_text: str,
    embedding: list[float],
    top_k: int,
):
    """Hybrid retrieval — vector ANN + BM25, fused via RRF.

    Pulls ``top_k * 3`` candidates from each side (a wider pool than the
    final cut so the fusion has signal beyond the obvious top hits), fuses
    their rankings with :func:`_rrf_fuse` (k=60), then materialises the
    top ``top_k`` rows in fused order.

    Returned row shape matches :func:`_retrieve_vector`:
    ``(path, commit_sha, content, content_type)``. Callers don't need to
    know which path produced the rows.
    """
    fetch_n = top_k * 3
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        register_vector(conn)
        # Vector candidates — same query as the vector-only path, just
        # widened to top_k * 3.
        vector_rows = conn.execute(
            """
            SELECT id, path, commit_sha, content, content_type
            FROM chunks
            WHERE repo = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (repo, embedding, fetch_n),
        ).fetchall()
        # BM25 candidates — only consider rows whose tsvector matches the
        # parsed query at all (the @@ filter), then rank by ts_rank_cd.
        # plainto_tsquery handles tokenisation + stemming; identifiers like
        # `process_batch` are split by `_` under the english config which
        # is fine for our use case (the integration test uses an
        # underscore-free literal as the BM25 marker).
        bm25_rows = conn.execute(
            """
            SELECT id, path, commit_sha, content, content_type
            FROM chunks
            WHERE repo = %s
              AND content_tsv @@ plainto_tsquery('english', %s)
            ORDER BY ts_rank_cd(content_tsv, plainto_tsquery('english', %s)) DESC
            LIMIT %s
            """,
            (repo, query_text, query_text, fetch_n),
        ).fetchall()

    # Build an id -> row lookup for materialisation after fusion.
    rows_by_id: dict[int, tuple] = {}
    for row in vector_rows:
        rows_by_id[row[0]] = row[1:]
    for row in bm25_rows:
        rows_by_id.setdefault(row[0], row[1:])

    fused = _rrf_fuse(
        [
            [r[0] for r in vector_rows],
            [r[0] for r in bm25_rows],
        ],
    )
    return [rows_by_id[chunk_id] for chunk_id, _score in fused[:top_k]]


def _retrieve(repo: str, query_text: str, embedding: list[float], top_k: int):
    """Dispatcher — hybrid when enabled, vector-only otherwise.

    The signature is hybrid-shaped (carries ``query_text``) so the call
    site stays uniform across modes; the vector-only path simply ignores
    the text. Keeping this as the single entry point means task 12
    (reranker) can wrap it without forking on the flag.
    """
    if HYBRID_SEARCH_ENABLED:
        return _retrieve_hybrid(repo, query_text, embedding, top_k)
    return _retrieve_vector(repo, embedding, top_k)


# ---------------------------------------------------------------------------
# Rate-limit helpers (plan 14).
#
# Sliding-window token budgets enforced via SUM over rate_limit_usage
# rows younger than 1 hour. Two independent caps: per-guild and per-user.
# Any request exceeding either cap is rejected with 429. The gate runs
# at the TOP of /ask — BEFORE the query-cache lookup — so a rate-limited
# request cannot consume a cache hit.
#
# Graceful degrade: any DB error during the check is logged and treated
# as "allowed". A flaky DB should not take down /ask because of a
# rate-limit bug.
# ---------------------------------------------------------------------------


def _rate_limit_check(
    guild_id: str | None, user_id: str | None,
) -> tuple[bool, str | None, int]:
    """Return ``(allowed, reason, retry_after_secs)``.

    ``reason`` is one of ``None``, ``"guild_budget"``, ``"user_budget"``.
    ``retry_after_secs`` is the number of seconds until the oldest row
    currently in the 1-hour window ages out — the earliest point at
    which the bucket can plausibly drain below the cap. It is clamped to
    ``>= 1`` so clients never see a literal ``retry_after: 0``.

    Skips entirely when both identifiers are None (non-Discord caller).
    Guild cap is checked before user cap; whichever trips first wins —
    the caller gets a single reason label and we don't double-count.
    """
    # Non-Discord caller (curl probe, health-check flow) — skip the gate.
    if guild_id is None and user_id is None:
        return (True, None, 0)
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True) as conn:
            if guild_id is not None:
                row = conn.execute(
                    """
                    SELECT COALESCE(SUM(tokens), 0)
                    FROM rate_limit_usage
                    WHERE guild_id = %s
                      AND window_at > now() - interval '1 hour'
                    """,
                    (guild_id,),
                ).fetchone()
                guild_used = int(row[0]) if row else 0
                if guild_used >= GUILD_TOKENS_PER_HOUR:
                    return (
                        False,
                        "guild_budget",
                        _retry_after_secs(conn, "guild_id", guild_id),
                    )
            if user_id is not None:
                row = conn.execute(
                    """
                    SELECT COALESCE(SUM(tokens), 0)
                    FROM rate_limit_usage
                    WHERE user_id = %s
                      AND window_at > now() - interval '1 hour'
                    """,
                    (user_id,),
                ).fetchone()
                user_used = int(row[0]) if row else 0
                if user_used >= USER_TOKENS_PER_HOUR:
                    return (
                        False,
                        "user_budget",
                        _retry_after_secs(conn, "user_id", user_id),
                    )
    except Exception:
        log.exception("rate_limit_check failed; allowing request (graceful degrade)")
        return (True, None, 0)
    return (True, None, 0)


def _retry_after_secs(conn, column: str, value: str) -> int:
    """Seconds until the oldest in-window row for ``column=value`` ages out.

    SELECTs ``MIN(window_at)`` for the bucket; returns
    ``max(1, 3600 - (now - min_window_at))``. On any error or empty
    bucket (shouldn't happen because we only call this after finding
    over-budget usage), falls back to 60s so clients have a sane retry.
    """
    try:
        sql = (
            f"SELECT EXTRACT(EPOCH FROM (now() - MIN(window_at))) "
            f"FROM rate_limit_usage "
            f"WHERE {column} = %s "
            f"  AND window_at > now() - interval '1 hour'"
        )
        row = conn.execute(sql, (value,)).fetchone()
        if not row or row[0] is None:
            return 60
        age = float(row[0])
        return max(1, int(3600 - age))
    except Exception:
        log.exception("retry_after lookup failed; defaulting to 60s")
        return 60


def _record_rate_limit_usage(
    guild_id: str | None,
    user_id: str | None,
    repo: str,
    tokens: int,
) -> None:
    """Insert one row recording `tokens` spent by (guild_id, user_id, repo).

    Skipped when both ids are None (non-Discord caller — we never
    gate those and therefore never need to account them either). Empty
    strings are coerced for the NOT NULL columns so a partial identity
    (guild but no user, or vice versa) still accounts correctly.

    DB errors are swallowed so a wedged Postgres cannot poison a
    successful /ask — we'd rather serve the answer and miss one usage
    row than 500 the user after they've already paid for the tokens.
    """
    if guild_id is None and user_id is None:
        return
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO rate_limit_usage (guild_id, user_id, repo, tokens)
                VALUES (%s, %s, %s, %s)
                """,
                (guild_id or "", user_id or "", repo, int(tokens)),
            )
    except Exception:
        log.exception("rate_limit_usage insert failed; usage not recorded")


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    start = time.perf_counter()
    status = "error"
    hits = 0
    prompt_tokens = 0
    completion_tokens = 0
    cache_commit_sha: str | None = None
    qhash: str | None = None
    # ------------------------------------------------------------------
    # Pre-retrieval gates — keep this region short. Rate limiting sits
    # ABOVE the query cache: a rate-limited request must not consume a
    # cache hit either, so the gate comes first. Anything below this
    # point is the slow path.
    # ------------------------------------------------------------------
    # Rate limiting (plan 14). Applies only when the caller sent a
    # (guild_id, user_id). Non-Discord callers (curl probes) bypass.
    if RATE_LIMIT_ENABLED:
        allowed, reason, retry_after = _rate_limit_check(req.guild_id, req.user_id)
        if not allowed:
            metrics.RATE_LIMIT_HITS_TOTAL.labels(req.repo, reason or "unknown").inc()
            log.info(
                "ask rate limited",
                extra={
                    "event": "ask.rate_limited",
                    "repo": req.repo,
                    "reason": reason,
                    "retry_after": retry_after,
                    "guild_id": req.guild_id,
                    "user_id": req.user_id,
                },
            )
            # Record the 429 outcome so the standard /ask metrics stay
            # coherent (total = ok + empty + error + cache_hit +
            # rate_limited). Status "rate_limited" is a new label value
            # — Prometheus handles this automatically.
            elapsed = time.perf_counter() - start
            metrics.LATENCY_SECONDS.labels("ask").observe(elapsed)
            metrics.QUERIES_TOTAL.labels(req.repo, "rate_limited").inc()
            return JSONResponse(
                status_code=429,
                content={"error": reason, "retry_after": retry_after},
            )
    # Query cache (plan 13). Compute the hash once so both the lookup
    # and the later INSERT share the same value. `_latest_commit_sha`
    # returns None before the first successful ingestion — in that case
    # there is nothing that could have been cached, so fall through.
    qhash = _query_hash(req.query)
    if QUERY_CACHE_ENABLED:
        cache_commit_sha = _latest_commit_sha(req.repo)
        if cache_commit_sha is not None:
            row: tuple | None = None
            try:
                with psycopg.connect(
                    PG_DSN, connect_timeout=3, autocommit=True
                ) as conn:
                    row = conn.execute(
                        """
                        SELECT answer, citations
                        FROM query_cache
                        WHERE repo = %s AND commit_sha = %s AND query_hash = %s
                        """,
                        (req.repo, cache_commit_sha, qhash),
                    ).fetchone()
                    if row is not None:
                        conn.execute(
                            """
                            UPDATE query_cache
                               SET hits = hits + 1
                             WHERE repo = %s
                               AND commit_sha = %s
                               AND query_hash = %s
                            """,
                            (req.repo, cache_commit_sha, qhash),
                        )
            except Exception:
                log.exception(
                    "query_cache lookup failed; falling through to slow path"
                )
                row = None
            if row is not None:
                status = "cache_hit"
                answer_text, citations_json = row
                metrics.CACHE_HITS_TOTAL.labels(req.repo).inc()
                log.info(
                    "ask served from cache",
                    extra={
                        "event": "ask.cache_hit",
                        "repo": req.repo,
                        "commit_sha": cache_commit_sha,
                    },
                )
                try:
                    # citations is JSONB — psycopg decodes to list/dict already.
                    # Fall back to json.loads if a driver returns the raw text.
                    if isinstance(citations_json, (str, bytes, bytearray)):
                        citations_raw = json.loads(citations_json)
                    else:
                        citations_raw = citations_json
                    citations = [Citation(**c) for c in citations_raw]
                    elapsed = time.perf_counter() - start
                    metrics.LATENCY_SECONDS.labels("ask").observe(elapsed)
                    metrics.QUERIES_TOTAL.labels(req.repo, status).inc()
                    # Cache hit costs the user zero tokens against
                    # their budget. The row still exists so "how many
                    # times has this guild asked?" stays meaningful if
                    # we ever want a request-count cap in addition to
                    # a token cap.
                    if RATE_LIMIT_ENABLED:
                        _record_rate_limit_usage(
                            req.guild_id, req.user_id, req.repo, 0,
                        )
                    return AskResponse(answer=answer_text, citations=citations)
                except Exception:
                    # Corrupt cache row — log and fall through to the
                    # normal path rather than 500-ing on the user.
                    log.exception(
                        "query_cache row decode failed; falling through"
                    )
            else:
                metrics.CACHE_MISSES_TOTAL.labels(req.repo).inc()
    # Resolve the active model per-request (plan 17). No env-var fallback —
    # instance must have had /model set run explicitly. None here short-
    # circuits with a 409 so the bot can surface "model not set" to the
    # user instead of routing traffic to a misconfigured backend.
    model = _get_chat_model_for_repo(req.repo)
    log.info(
        "ask request received",
        extra={
            "event": "ask.received",
            "repo": req.repo,
            "query_chars": len(req.query),
        },
    )
    if model is None:
        status = "unset"
        log.info(
            "ask rejected: chat model not set for repo",
            extra={"event": "ask.unset", "repo": req.repo},
        )
        metrics.QUERIES_TOTAL.labels(req.repo, status).inc()
        metrics.LATENCY_SECONDS.labels("ask").observe(time.perf_counter() - start)
        return JSONResponse(
            status_code=409,
            content={
                "error": "model not set",
                "action": "a server admin must run /model set <name> "
                          "(see /model list for options)",
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

        # Rerank-aware retrieval width: when the cross-encoder is wired up,
        # we pull a wider initial pool (top_k * RERANKER_MULT) so the
        # reranker has enough candidates to actually re-order. When it is
        # off, behaviour is identical to before (just top_k).
        rerank_active = bool(RERANKER_ENABLED and RERANKER_URL)
        retrieve_n = req.top_k * RERANKER_MULT if rerank_active else req.top_k
        rows = _retrieve(req.repo, req.query, emb, retrieve_n)

        if rerank_active and rows:
            # Convert the tuple rows to dicts for the reranker, then back
            # again, keeping every original field intact. The reranker is
            # responsible for graceful degrade — it returns the input list
            # unchanged on any failure — so we always end up with a usable
            # `rows` value here.
            candidates = [
                {
                    "path": p,
                    "commit_sha": s,
                    "content": c,
                    "content_type": ctype,
                }
                for p, s, c, ctype in rows
            ]
            try:
                with metrics.timed(metrics.RERANK_LATENCY_SECONDS):
                    reranked = asyncio.run(
                        reranker_mod.rerank(
                            req.query,
                            candidates,
                            url=RERANKER_URL,
                            model=RERANKER_MODEL,
                        )
                    )
            except Exception:
                # Defensive backstop — `rerank()` already swallows its own
                # failures, but if a programming error or unexpected
                # exception escapes (e.g. asyncio loop weirdness), we still
                # want /ask to succeed with the un-reranked rows.
                log.exception("rerank call raised; falling back to retrieval order")
                reranked = candidates
            rows = [
                (c["path"], c["commit_sha"], c["content"], c["content_type"])
                for c in reranked[: req.top_k]
            ]

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
        answer_text = completion.choices[0].message.content or ""
        citations = [Citation(path=p, commit_sha=s) for p, s, _, _ in rows]

        # Write-through cache (plan 13). Only on the "ok" path with
        # non-empty citations — empty/error responses are never cached.
        # A concurrent /ask racing this INSERT is harmless: both answers
        # are valid for the same commit_sha, ON CONFLICT DO NOTHING
        # keeps whichever arrived first.
        if QUERY_CACHE_ENABLED and cache_commit_sha is not None and citations:
            try:
                with psycopg.connect(
                    PG_DSN, connect_timeout=3, autocommit=True
                ) as conn:
                    conn.execute(
                        """
                        INSERT INTO query_cache
                            (repo, commit_sha, query_hash, answer, citations)
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (repo, commit_sha, query_hash) DO NOTHING
                        """,
                        (
                            req.repo,
                            cache_commit_sha,
                            qhash,
                            answer_text,
                            json.dumps(
                                [c.model_dump() for c in citations]
                            ),
                        ),
                    )
            except Exception:
                log.exception(
                    "query_cache insert failed; answer still returned"
                )

        # Rate-limit accounting (plan 14). Record actual token usage
        # against the (guild, user) pair so the next hour's budget
        # check reflects this call. No-op when the feature is off or
        # the caller didn't send a Discord identity.
        if RATE_LIMIT_ENABLED:
            _record_rate_limit_usage(
                req.guild_id,
                req.user_id,
                req.repo,
                int(prompt_tokens) + int(completion_tokens),
            )

        return AskResponse(answer=answer_text, citations=citations)
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
        # Sanitised external message; the full psycopg exception is in logs.
        raise HTTPException(status_code=503, detail="database unavailable") from exc

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
        raise HTTPException(status_code=503, detail="database unavailable") from exc
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
        raise HTTPException(status_code=503, detail="database unavailable") from exc
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
