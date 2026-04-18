"""Prometheus metrics for the rag orchestrator.

Kept in its own module so `app.py` stays focused on request handling. All
metrics live on the default `prometheus_client` global registry so
`generate_latest()` in the /metrics handler covers everything at once.

Name prefix: `gitdoc_`. Every business metric is labelled by `repo` so the
homelab Prometheus can fan-out per-instance dashboards without reserving
one series per chart release.
"""
from __future__ import annotations

import time
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

# --- Counters ---------------------------------------------------------------

QUERIES_TOTAL = Counter(
    "gitdoc_queries_total",
    "Total /ask requests processed, labelled by repo and outcome.",
    ("repo", "status"),
)

TOKENS_PROMPT_TOTAL = Counter(
    "gitdoc_tokens_prompt_total",
    "Total prompt tokens consumed by chat completions, per repo and model.",
    ("repo", "model"),
)

TOKENS_COMPLETION_TOTAL = Counter(
    "gitdoc_tokens_completion_total",
    "Total completion tokens emitted by chat completions, per repo and model.",
    ("repo", "model"),
)

# Query cache (plan 13). Hit/miss split per repo so operators can compute
# the hit rate as `rate(hits) / (rate(hits) + rate(misses))` — that's the
# "is the cache actually earning its keep?" dashboard tile. No latency
# metric: cache hits skip the slow path entirely, so `gitdoc_latency_seconds`
# already tells that story.
CACHE_HITS_TOTAL = Counter(
    "gitdoc_cache_hits_total",
    "Query cache hits",
    ("repo",),
)

CACHE_MISSES_TOTAL = Counter(
    "gitdoc_cache_misses_total",
    "Query cache misses",
    ("repo",),
)

# Rate-limit hits (plan 14). Incremented every time /ask rejects a request
# because its (guild_id or user_id) token budget is exhausted. `reason` is
# `guild_budget` or `user_budget` so the homelab Prometheus can split
# dashboards into "is one guild hot?" vs "is one user hammering?". No
# latency / error metric — 429 is a fast path.
RATE_LIMIT_HITS_TOTAL = Counter(
    "gitdoc_rate_limit_hits_total",
    "Requests rejected because they exceeded a token budget",
    ("repo", "reason"),  # reason: "guild_budget" | "user_budget"
)

# --- Histograms -------------------------------------------------------------

# Retrieval returns between 0 and top_k rows (top_k<=20). Buckets picked to
# give useful answer-quality alerts: 0 == nothing relevant was found.
RETRIEVAL_HITS = Histogram(
    "gitdoc_retrieval_hits",
    "Number of context rows returned by vector retrieval per /ask request.",
    ("repo",),
    buckets=(0, 1, 3, 5, 10, 20),
)

LATENCY_SECONDS = Histogram(
    "gitdoc_latency_seconds",
    "End-to-end wall-clock latency of HTTP handlers, labelled by endpoint.",
    ("endpoint",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30),
)

EMBED_LATENCY_SECONDS = Histogram(
    "gitdoc_embed_latency_seconds",
    "Latency of the embedding backend call made during /ask.",
    ("repo",),
)

CHAT_LATENCY_SECONDS = Histogram(
    "gitdoc_chat_latency_seconds",
    "Latency of the chat-completion backend call made during /ask.",
    ("repo", "model"),
)

# Cross-encoder reranker (plan 12). The budget for a healthy bge-reranker
# call on top_k=6 candidates (so 18 docs at the default candidatesMultiplier
# of 3) is roughly 200ms-500ms p95 on a small GPU. Buckets cover both the
# happy path and the "something is wrong" tail (multi-second latencies are
# how a wedged backend manifests before timeouts trip).
RERANK_LATENCY_SECONDS = Histogram(
    "gitdoc_rerank_latency_seconds",
    "Time spent in the cross-encoder reranker",
    buckets=(0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0),
)


# --- Small helpers ----------------------------------------------------------


@contextmanager
def timed(histogram, *label_values):
    """Context manager that observes elapsed seconds into `histogram`.

    Used as thin wrappers around the embedding / chat / per-request blocks
    in `app.py` so the handler body stays readable. `time.perf_counter()`
    is monotonic and has ns resolution on every platform we care about.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        if label_values:
            histogram.labels(*label_values).observe(elapsed)
        else:
            histogram.observe(elapsed)


def record_chat_usage(completion, repo: str, model: str) -> tuple[int, int]:
    """Extract token usage from an OpenAI-compatible completion.

    Returns `(prompt_tokens, completion_tokens)` as ints — zero when the
    backend didn't return usage (some LiteLLM routes, Ollama). The counter
    increments are safe to skip in that case: a zero delta is a no-op, and
    the `None` path is what we want metric-side to avoid silently inflating
    the real number.
    """
    usage = getattr(completion, "usage", None)
    if usage is None:
        return 0, 0
    prompt = getattr(usage, "prompt_tokens", None) or 0
    completion_tokens = getattr(usage, "completion_tokens", None) or 0
    if prompt:
        TOKENS_PROMPT_TOTAL.labels(repo, model).inc(prompt)
    if completion_tokens:
        TOKENS_COMPLETION_TOTAL.labels(repo, model).inc(completion_tokens)
    return prompt, completion_tokens
