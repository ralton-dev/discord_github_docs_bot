import logging
import os
import pathlib
import subprocess
import tempfile
from urllib.parse import urlparse, urlunparse

import psycopg
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter
from openai import OpenAI
from pgvector.psycopg import register_vector

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("gitdoc.ingest")

REPO_URL     = os.environ["REPO_URL"]
REPO_NAME    = os.environ["REPO_NAME"]
BRANCH       = os.environ.get("REPO_BRANCH", "main")
PG_DSN       = os.environ["POSTGRES_DSN"]
GIT_TOKEN    = os.environ.get("GIT_TOKEN", "")
LITELLM_BASE = os.environ["LITELLM_BASE_URL"]
LITELLM_KEY  = os.environ["LITELLM_API_KEY"]
EMBED_MODEL  = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
BATCH_SIZE   = int(os.environ.get("EMBED_BATCH", "64"))

llm = OpenAI(base_url=LITELLM_BASE, api_key=LITELLM_KEY)

EXT_LANG: dict[str, Language] = {
    ".py":   Language.PYTHON,
    ".js":   Language.JS,
    ".jsx":  Language.JS,
    ".ts":   Language.TS,
    ".tsx":  Language.TS,
    ".go":   Language.GO,
    ".java": Language.JAVA,
    ".rs":   Language.RUST,
    ".rb":   Language.RUBY,
    ".php":  Language.PHP,
    ".cpp":  Language.CPP,
    ".c":    Language.CPP,
    ".cs":   Language.CSHARP,
    ".kt":   Language.KOTLIN,
    ".scala": Language.SCALA,
    ".swift": Language.SWIFT,
}
DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc"}
SKIP_DIRS = {".git", "node_modules", "dist", "build", "vendor",
             "__pycache__", ".venv", "venv", "target", "out"}
MAX_FILE_BYTES = 500_000


def _auth_url(url: str, token: str) -> str:
    if not token:
        return url
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        return url
    return urlunparse(u._replace(netloc=f"x-access-token:{token}@{u.hostname}"
                                 + (f":{u.port}" if u.port else "")))


def clone(dest: pathlib.Path) -> str:
    subprocess.run(
        ["git", "clone", "--depth=1", "--branch", BRANCH,
         _auth_url(REPO_URL, GIT_TOKEN), str(dest)],
        check=True,
    )
    return subprocess.check_output(
        ["git", "-C", str(dest), "rev-parse", "HEAD"]
    ).decode().strip()


def splitter_for(path: pathlib.Path) -> RecursiveCharacterTextSplitter:
    lang = EXT_LANG.get(path.suffix)
    if lang is not None:
        return RecursiveCharacterTextSplitter.from_language(
            language=lang, chunk_size=1200, chunk_overlap=150
        )
    return RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)


def iter_chunks(root: pathlib.Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix not in EXT_LANG and p.suffix not in DOC_EXT:
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                log.info("skipping large file %s", p)
                continue
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(p.relative_to(root))
        ctype = "code" if p.suffix in EXT_LANG else "markdown"
        language = p.suffix.lstrip(".") if p.suffix in EXT_LANG else None
        for i, chunk in enumerate(splitter_for(p).split_text(text)):
            yield rel, i, chunk, ctype, language


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="repo-") as tmp:
        root = pathlib.Path(tmp)
        sha = clone(root)
        log.info("cloned %s@%s", REPO_NAME, sha)

        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            register_vector(conn)
            run_id = conn.execute(
                "INSERT INTO ingest_runs (repo, commit_sha) VALUES (%s,%s) RETURNING id",
                (REPO_NAME, sha),
            ).fetchone()[0]

            total = 0
            batch: list[tuple] = []

            def flush(batch: list[tuple]):
                nonlocal total
                if not batch:
                    return
                embs = llm.embeddings.create(
                    model=EMBED_MODEL, input=[b[2] for b in batch]
                ).data
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO chunks (repo, path, commit_sha, chunk_index,
                                            content, content_type, language, embedding)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (repo, path, commit_sha, chunk_index) DO NOTHING
                        """,
                        [
                            (REPO_NAME, b[0], sha, b[1], b[2], b[3], b[4], e.embedding)
                            for b, e in zip(batch, embs)
                        ],
                    )
                total += len(batch)
                log.info("upserted %d chunks (total=%d)", len(batch), total)

            try:
                for row in iter_chunks(root):
                    batch.append(row)
                    if len(batch) >= BATCH_SIZE:
                        flush(batch)
                        batch = []
                flush(batch)

                deleted = conn.execute(
                    "DELETE FROM chunks WHERE repo=%s AND commit_sha<>%s",
                    (REPO_NAME, sha),
                ).rowcount
                log.info("garbage-collected %d old chunks", deleted)

                conn.execute(
                    "UPDATE ingest_runs SET finished_at=now(), chunk_count=%s, status='ok' "
                    "WHERE id=%s",
                    (total, run_id),
                )
            except Exception:
                conn.execute(
                    "UPDATE ingest_runs SET finished_at=now(), status='failed' WHERE id=%s",
                    (run_id,),
                )
                raise


if __name__ == "__main__":
    main()
