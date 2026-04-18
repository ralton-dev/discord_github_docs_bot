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
