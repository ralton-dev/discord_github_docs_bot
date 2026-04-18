# Architecture

The database uses pgvector for similarity search across the embedded chunks.
This is the only file in the fixture that mentions the storage backend choice,
so a citation test can pin the answer to this exact path.

Sentinel marker: `bilby_architecture_marker_99`.

## Components

- **ingest** — clones the repo and writes chunks + embeddings.
- **rag** — embeds queries and retrieves nearest neighbours.
- **discord-bot** — thin Discord adapter on top of the orchestrator HTTP API.
