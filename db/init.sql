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

-- Query cache (plan 13). Short-circuits /ask when the same normalized
-- query has been answered against the same commit_sha. The (repo,
-- commit_sha, query_hash) primary key is the natural cache key; rows are
-- invalidated when a new ingestion run produces a new commit_sha for the
-- repo (see services/ingestion/ingest.py GC step). `citations` mirrors
-- the /ask response shape as a JSONB array of {path, commit_sha}. `hits`
-- is bumped on each cache hit for cheap "which answers are popular"
-- analytics without needing a separate metric-side query.
CREATE TABLE IF NOT EXISTS query_cache (
    repo        TEXT NOT NULL,
    commit_sha  TEXT NOT NULL,
    query_hash  TEXT NOT NULL,
    answer      TEXT NOT NULL,
    citations   JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    hits        INT NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, commit_sha, query_hash)
);
CREATE INDEX IF NOT EXISTS query_cache_repo_created_idx
    ON query_cache (repo, created_at DESC);

-- Rate-limit usage (plan 14). One row per successful /ask request that
-- carried a guild_id and/or user_id. The sliding-window check sums
-- `tokens` over rows younger than 1 hour for the relevant guild_id /
-- user_id; cache hits record tokens=0 so cached answers are free.
-- No eviction is wired here: the indexes make the `window_at > now() -
-- interval '1 hour'` filter cheap, and rows outside the window are
-- effectively ignored. A janitor DELETE every day is fine if the table
-- grows beyond comfort.
CREATE TABLE IF NOT EXISTS rate_limit_usage (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    repo        TEXT NOT NULL,
    tokens      INT NOT NULL,
    window_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rate_limit_usage_guild_window_idx
    ON rate_limit_usage (guild_id, window_at DESC);
CREATE INDEX IF NOT EXISTS rate_limit_usage_user_window_idx
    ON rate_limit_usage (user_id, window_at DESC);
