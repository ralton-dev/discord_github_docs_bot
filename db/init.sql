-- Canonical schema. The Helm chart ships the same SQL templated with the
-- configured embedding dimension; this copy is for local dev / reference.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id            BIGSERIAL PRIMARY KEY,
    repo          TEXT NOT NULL,
    path          TEXT NOT NULL,
    commit_sha    TEXT NOT NULL,
    chunk_index   INT  NOT NULL,
    content       TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    language      TEXT,
    token_count   INT,
    embedding     vector(1536) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (repo, path, commit_sha, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_repo_idx ON chunks (repo);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- Hybrid search (plan 11). The generated tsvector column stays in sync with
-- `content` automatically on every INSERT/UPDATE — no trigger or
-- application-side bookkeeping. The GIN index supports BM25-style ranking
-- via `ts_rank_cd(content_tsv, plainto_tsquery('english', $query))`.
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx
    ON chunks USING gin (content_tsv);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id            BIGSERIAL PRIMARY KEY,
    repo          TEXT NOT NULL,
    commit_sha    TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    chunk_count   INT,
    status        TEXT NOT NULL DEFAULT 'running'
);

-- Per-instance mutable settings. Currently only chat_model, set via the
-- Discord bot's /model set command and read by the /ask handler on every
-- request (with a short-lived in-process cache). updated_by records the
-- Discord user ID for audit/rotation.
CREATE TABLE IF NOT EXISTS instance_settings (
    repo        TEXT PRIMARY KEY,
    chat_model  TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT
);
