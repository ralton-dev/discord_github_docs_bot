import logging
import os

import psycopg
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pgvector.psycopg import register_vector
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("gitdoc.rag")

LITELLM_BASE = os.environ["LITELLM_BASE_URL"]
LITELLM_KEY  = os.environ["LITELLM_API_KEY"]
PG_DSN       = os.environ["POSTGRES_DSN"]
EMBED_MODEL  = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
CHAT_MODEL   = os.environ.get("CHAT_MODEL",  "claude-opus-4-7")

SYSTEM_PROMPT = """You are an assistant for a software project. Answer questions
using ONLY the provided context from the project's documentation and code.

Rules:
- If the answer is not in the context, say you don't know. Do not speculate.
- Quote file paths when you cite specific details.
- Be concise. Prefer short answers with code examples when relevant.
"""

llm = OpenAI(base_url=LITELLM_BASE, api_key=LITELLM_KEY)
app = FastAPI(title="gitdoc-rag")


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
    try:
        emb = llm.embeddings.create(model=EMBED_MODEL, input=req.query).data[0].embedding
    except Exception as exc:
        log.exception("embedding call failed")
        raise HTTPException(status_code=502, detail="embedding backend unavailable") from exc

    rows = _retrieve(req.repo, emb, req.top_k)
    if not rows:
        return AskResponse(
            answer="I couldn't find anything relevant in the knowledge base for that question.",
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
        completion = llm.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1024,
        )
    except Exception as exc:
        log.exception("chat call failed")
        raise HTTPException(status_code=502, detail="chat backend unavailable") from exc

    return AskResponse(
        answer=completion.choices[0].message.content or "",
        citations=[Citation(path=p, commit_sha=s) for p, s, _, _ in rows],
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
