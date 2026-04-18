import base64
import logging
import os
import pathlib
import subprocess
import tempfile
import time

import psycopg
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter
from openai import OpenAI
from pgvector.psycopg import register_vector

from logging_config import configure as configure_logging

configure_logging()
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
# Skip the check and re-embed every chunk even when the SHA hasn't moved.
# Use when bumping chunking rules, swapping embedding model (on a fresh
# DB), or recovering from a broken run. The natural invariant is "no
# work when the SHA is unchanged", so default is off.
FORCE_REINGEST = os.environ.get("FORCE_REINGEST", "").lower() in (
    "1", "true", "yes", "on",
)

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


def _git_auth_env(base_env: "os._Environ | dict[str, str]", token: str) -> dict[str, str]:
    """Build a subprocess env that authenticates git HTTPS WITHOUT putting
    the token in argv, the URL, or anywhere git itself might echo.

    Uses git's GIT_CONFIG_COUNT / GIT_CONFIG_KEY_<N> / GIT_CONFIG_VALUE_<N>
    env-var pattern (git >= 2.31) to inject `http.extraheader:
    Authorization: Basic <base64(x-access-token:TOKEN)>` for this
    subprocess only. The header is attached to the HTTPS request to the
    remote — git never logs headers, only URLs. Pair with a credential-free
    URL passed to `git clone` so even git's fatal: error messages carry no
    secret.

    Returns a fresh dict so the caller's env isn't mutated.
    """
    env = dict(base_env)
    if token:
        auth = base64.b64encode(
            f"x-access-token:{token}".encode("utf-8")
        ).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {auth}"
    return env


def _scrub_token(text: str, token: str) -> str:
    """Redact `token` from arbitrary text. Defence-in-depth: the
    Authorization-header flow above keeps the token out of git's output
    entirely, but if a future git version or plugin ever leaks it via
    stderr, don't let that land in Loki / audit logs unredacted.
    """
    if not token or not text:
        return text
    return text.replace(token, "<redacted>")


def clone(dest: pathlib.Path) -> str:
    env = _git_auth_env(os.environ, GIT_TOKEN)
    # --quiet suppresses normal progress noise; stderr is captured so we
    # can scrub + log it ourselves on failure. The URL passed to git has
    # NO credentials in it — the token rides in an out-of-band header.
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", "--quiet", "--branch", BRANCH,
             REPO_URL, str(dest)],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = _scrub_token(exc.stderr or "", GIT_TOKEN)
        # `from None` drops the original CalledProcessError from the
        # traceback; its __repr__ includes argv, which future code could
        # theoretically extend to embed a secret again.
        raise RuntimeError(
            f"git clone failed (exit {exc.returncode}): {stderr}"
        ) from None
    return subprocess.check_output(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        env=env,
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
                log.info(
                    "skipping large file %s" % p,
                    extra={"event": "ingest.skip_large", "path": str(p)},
                )
                continue
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(p.relative_to(root))
        ctype = "code" if p.suffix in EXT_LANG else "markdown"
        language = p.suffix.lstrip(".") if p.suffix in EXT_LANG else None
        for i, chunk in enumerate(splitter_for(p).split_text(text)):
            yield rel, i, chunk, ctype, language


def _already_ingested(conn, repo: str, sha: str) -> bool:
    """Return True if a prior run for ``(repo, sha)`` completed successfully.

    Used to short-circuit the scheduled CronJob when `repo.branch` hasn't
    advanced since the last run — without this check, every tick clones
    the repo, re-embeds every chunk (paying the full embedding-API cost),
    and then watches ON CONFLICT DO NOTHING discard the rows because they
    already exist for this SHA. Idempotent, but wasteful.

    Only an ``ok`` status counts: a ``running``/``failed`` row from a
    prior crash means the previous run didn't finish, and we want the
    current run to re-try.
    """
    row = conn.execute(
        "SELECT 1 FROM ingest_runs "
        "WHERE repo = %s AND commit_sha = %s AND status = 'ok' "
        "LIMIT 1",
        (repo, sha),
    ).fetchone()
    return row is not None


def main() -> None:
    started = time.perf_counter()
    log.info(
        "ingest start",
        extra={
            "event": "ingest.start",
            "repo": REPO_NAME,
            "url": REPO_URL,
            "branch": BRANCH,
        },
    )
    with tempfile.TemporaryDirectory(prefix="repo-") as tmp:
        root = pathlib.Path(tmp)
        sha = clone(root)
        log.info(
            "cloned %s@%s" % (REPO_NAME, sha),
            extra={
                "event": "ingest.cloned",
                "repo": REPO_NAME,
                "sha": sha,
            },
        )

        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            register_vector(conn)

            # Fast-exit when the branch hasn't moved since the last ok run.
            # Cheap — one indexed lookup against ingest_runs — and saves
            # an entire walk + embed + upsert cycle. Operator can bypass
            # via FORCE_REINGEST=true (see README/docs/configuration.md).
            if not FORCE_REINGEST and _already_ingested(conn, REPO_NAME, sha):
                log.info(
                    "ingest skipped — commit already fully ingested",
                    extra={
                        "event": "ingest.skipped",
                        "repo": REPO_NAME,
                        "sha": sha,
                        "duration_ms": int(
                            (time.perf_counter() - started) * 1000
                        ),
                    },
                )
                return

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
                log.info(
                    "upserted %d chunks (total=%d)" % (len(batch), total),
                    extra={
                        "event": "ingest.batch",
                        "repo": REPO_NAME,
                        "batch_size": len(batch),
                        "total": total,
                    },
                )

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
                log.info(
                    "garbage-collected %d old chunks" % deleted,
                    extra={
                        "event": "ingest.gc",
                        "repo": REPO_NAME,
                        "deleted": deleted,
                    },
                )

                # Query cache (plan 13) is keyed on (repo, commit_sha,
                # query_hash); a new commit_sha makes every prior cached
                # answer potentially stale. Drop them so the next /ask
                # re-runs retrieval against the fresh chunks.
                cache_deleted = conn.execute(
                    "DELETE FROM query_cache WHERE repo = %s AND commit_sha <> %s",
                    (REPO_NAME, sha),
                ).rowcount
                log.info(
                    "invalidated %d stale query_cache rows" % cache_deleted,
                    extra={
                        "event": "ingest.cache_invalidated",
                        "repo": REPO_NAME,
                        "deleted": cache_deleted,
                    },
                )

                conn.execute(
                    "UPDATE ingest_runs SET finished_at=now(), chunk_count=%s, status='ok' "
                    "WHERE id=%s",
                    (total, run_id),
                )
                duration_ms = int((time.perf_counter() - started) * 1000)
                log.info(
                    "ingest complete",
                    extra={
                        "event": "ingest.complete",
                        "repo": REPO_NAME,
                        "sha": sha,
                        "chunks": total,
                        "deleted": deleted,
                        "duration_ms": duration_ms,
                    },
                )
            except Exception:
                duration_ms = int((time.perf_counter() - started) * 1000)
                conn.execute(
                    "UPDATE ingest_runs SET finished_at=now(), status='failed' WHERE id=%s",
                    (run_id,),
                )
                log.exception(
                    "ingest failed",
                    extra={
                        "event": "ingest.failed",
                        "repo": REPO_NAME,
                        "sha": sha,
                        "duration_ms": duration_ms,
                    },
                )
                raise


if __name__ == "__main__":
    main()
