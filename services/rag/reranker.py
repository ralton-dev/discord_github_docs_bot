"""Cross-encoder reranker (plan 12).

Wraps the output of `_retrieve` in `app.py` with a cross-encoder relevance
scoring pass before the LLM ever sees the context. Vector similarity (and
even hybrid + BM25) returns "broadly relevant" candidates; a cross-encoder
reads `(query, chunk)` jointly and produces a much more discriminating
score. Standard recipe: fetch ``top_k * N`` candidates, rerank, keep the
top ``top_k``.

## Pluggable HTTP shape

LiteLLM's rerank endpoint coverage is patchy and Ollama itself does not
expose a first-class `/api/rerank` route on every build. Rather than bind
to one upstream, this module speaks a small, generic shape:

    POST <RERANKER_URL>
    {
        "model":     "<reranker model name>",
        "query":     "<user query>",
        "documents": [{"text": "<chunk content>"}, ...]
    }

    => {"scores": [<float>, ...]}     # same length and order as `documents`

If the operator's reranker speaks a different shape (e.g. LiteLLM's
``/v1/rerank`` returns ``{"results": [{"index": int, "relevance_score":
float}, ...]}``), they point ``RERANKER_URL`` at a tiny adapter that
re-marshals the response. Documented in ``docs/retrieval.md``.

## Graceful degrade

The reranker is an *enhancement* — `/ask` MUST keep working when it goes
down. Every error path here logs a warning and returns the input
candidates unchanged so the caller can fall through to the un-reranked
ordering.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("gitdoc.rag.reranker")


async def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    url: str,
    model: str,
    timeout: float = 5.0,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Reorder ``candidates`` by cross-encoder relevance to ``query``.

    Parameters
    ----------
    query:
        The user question. Sent verbatim to the reranker.
    candidates:
        List of dicts. Each MUST contain a ``"content"`` key with the chunk
        text the reranker should score. All other fields (path, commit_sha,
        content_type, ...) are preserved on the output rows; only the order
        changes.
    url:
        Absolute URL of the reranker endpoint. Empty / falsy = caller is
        responsible for not invoking us.
    model:
        Model identifier sent in the request body (e.g.
        ``"BAAI/bge-reranker-v2-m3"``).
    timeout:
        Per-request HTTP timeout in seconds. Defaults to 5s — well above
        the ~500ms p95 budget for a 6-candidate batch on a healthy GPU,
        but tight enough that a wedged backend doesn't stall ``/ask``.
    client:
        Optional injected ``httpx.AsyncClient`` for tests. Production
        callers omit it; a per-call client is created inside the function
        with the configured timeout.

    Returns
    -------
    list[dict]
        ``candidates`` reordered by descending score. On any error
        (transport failure, malformed response, score/length mismatch) the
        original ``candidates`` list is returned unchanged.
    """
    if not candidates:
        return candidates

    payload = {
        "model": model,
        "query": query,
        "documents": [{"text": c.get("content", "")} for c in candidates],
    }

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as owned:
                resp = await owned.post(url, json=payload)
        else:
            resp = await client.post(url, json=payload, timeout=timeout)
    except (httpx.HTTPError, OSError) as exc:
        # Transport-layer failure — connection refused, DNS error, timeout,
        # TLS handshake failure, peer reset, etc. Log once and fall through.
        log.warning(
            "reranker transport error; returning unranked candidates: %s",
            exc,
            extra={"event": "rerank.transport_error", "error": str(exc)},
        )
        return candidates

    if resp.status_code >= 400:
        log.warning(
            "reranker HTTP %d; returning unranked candidates",
            resp.status_code,
            extra={
                "event": "rerank.http_error",
                "status_code": resp.status_code,
            },
        )
        return candidates

    try:
        body = resp.json()
    except ValueError as exc:
        log.warning(
            "reranker returned non-JSON body: %s",
            exc,
            extra={"event": "rerank.bad_json"},
        )
        return candidates

    scores = body.get("scores") if isinstance(body, dict) else None
    if not isinstance(scores, list) or len(scores) != len(candidates):
        log.warning(
            "reranker response malformed: expected 'scores' list of len %d, got %r",
            len(candidates),
            body,
            extra={
                "event": "rerank.malformed_response",
                "expected_len": len(candidates),
            },
        )
        return candidates

    # Coerce to float so a non-numeric score (which would crash sorting)
    # is treated as a malformed-response failure end-to-end.
    try:
        numeric_scores = [float(s) for s in scores]
    except (TypeError, ValueError) as exc:
        log.warning(
            "reranker scores not numeric: %s",
            exc,
            extra={"event": "rerank.non_numeric_scores"},
        )
        return candidates

    # Stable sort by descending score; ties preserve original order so the
    # vector / hybrid ordering acts as a tiebreaker — sensible default.
    paired = list(zip(numeric_scores, range(len(candidates)), candidates))
    paired.sort(key=lambda item: (-item[0], item[1]))
    return [c for _score, _idx, c in paired]
