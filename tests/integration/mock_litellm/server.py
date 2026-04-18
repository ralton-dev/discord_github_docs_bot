"""Tiny in-process OpenAI-compatible mock used by the integration tests.

Why a hand-rolled FastAPI mock and not the upstream LiteLLM image?
- The official LiteLLM image is ~500 MB and adds 10-30s of cold-start to every
  test session — well over half of our 2 minute wall-time budget.
- Our two endpoints have stable shapes (`/v1/embeddings`, `/v1/chat/completions`)
  and the openai SDK happily talks to anything that returns the right JSON.
- Determinism is the whole point: the embedding function below is a token-
  bag-of-words projection so query and source text that share salient terms
  rank close in cosine space.

Run via uvicorn programmatically (see `conftest.py`) — this module is never
imported by production code.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid

from fastapi import FastAPI
from pydantic import BaseModel

# Match what the rag/ingestion code expects (db schema is
# vector(1536); embeddings *must* match that dimensionality).
EMBED_DIM = 1536

# Tokens shorter than this are dropped — kills articles ("a", "an") and other
# noise that would otherwise dominate cosine similarity.
MIN_TOKEN_LEN = 3

# Stop-words pulled from a tiny English list. Removing them reduces noise so
# the bag-of-words embedding ranks the right file top-1 reliably.
_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "have", "what",
    "does", "into", "can", "are", "was", "were", "you", "your", "but",
    "not", "all", "any", "had", "has", "his", "her", "its", "they", "them",
    "who", "how", "why", "when", "where", "which", "will", "would", "should",
    "could", "there", "their", "these", "those", "than", "then", "some",
    "such", "only", "more", "most", "much", "many", "few", "one", "two",
    "use", "uses", "used", "using",
}


def _tokenise(text: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-zA-Z][a-zA-Z0-9_]*", text.lower())
        if len(t) >= MIN_TOKEN_LEN and t not in _STOPWORDS
    ]


def _token_to_dim(token: str) -> int:
    """Stable hash → dimension index. Same token always lands in same dim."""
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % EMBED_DIM


def deterministic_embedding(text: str) -> list[float]:
    """Bag-of-tokens projection into EMBED_DIM-dim space.

    Each token contributes weight 1.0 to its hashed dimension. The vector is
    then L2-normalised so cosine similarity reduces to dot product. Two texts
    sharing salient tokens will have a non-zero dot product; texts sharing no
    salient tokens will be orthogonal (and so equidistant from any query).

    A small constant baseline is added to dimension 0 so the all-zero edge
    case is impossible — pgvector's HNSW index rejects zero vectors when the
    cosine operator is configured, and we want the embedding to be defined
    for any non-empty input.
    """
    vec = [0.0] * EMBED_DIM
    vec[0] = 1e-6  # baseline so every vector has at least one non-zero dim

    for token in _tokenise(text):
        vec[_token_to_dim(token)] += 1.0

    # L2 normalise.
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


# ---------------------------------------------------------------------------
# Request / response models matching the OpenAI v1 API shape.
# ---------------------------------------------------------------------------

class EmbeddingsRequest(BaseModel):
    model: str
    input: str | list[str]


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None


def build_app() -> FastAPI:
    app = FastAPI(title="mock-litellm")

    @app.post("/v1/embeddings")
    def embeddings(req: EmbeddingsRequest):
        inputs = req.input if isinstance(req.input, list) else [req.input]
        data = [
            {"object": "embedding", "index": i,
             "embedding": deterministic_embedding(text)}
            for i, text in enumerate(inputs)
        ]
        return {
            "object": "list",
            "data": data,
            "model": req.model,
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    @app.post("/v1/chat/completions")
    def chat_completions(req: ChatCompletionRequest):
        # Echo back enough of the user prompt that the test can assert the
        # context made it through. We do NOT try to summarise — the test only
        # cares about citations, which the orchestrator builds from DB rows.
        user_msg = next(
            (m.content for m in reversed(req.messages) if m.role == "user"),
            "",
        )
        snippet = user_msg[:200].replace("\n", " ")
        answer = f"MOCK_RESPONSE answering based on context: {snippet}"
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


# Instantiated at import for `uvicorn tests.integration.mock_litellm.server:app`.
app = build_app()
