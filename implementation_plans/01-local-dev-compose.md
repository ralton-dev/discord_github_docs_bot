---
status: todo
phase: 1
priority: high
---

# 01 · Local dev compose

## Goal
Run the full stack (postgres+pgvector, rag-orchestrator, ingestion one-shot, optional bot) locally via `docker compose up` so changes can be validated without deploying to Kubernetes.

## Why
Tight feedback loop. Rebuilding images and pushing to a registry for every change is slow. Also makes it possible to write integration tests against a real database (task 07).

## Acceptance criteria
- [ ] `docker compose up -d db rag` boots a pgvector-enabled Postgres and the orchestrator; both pass health checks.
- [ ] `docker compose run --rm ingest` seeds the DB from a small test repo (can be this repo itself).
- [ ] `curl -XPOST localhost:8000/ask -d '{"query":"what is this project","repo":"self"}'` returns a grounded answer with citations.
- [ ] A `.env.example` at the repo root lists every var the stack needs; `.env` is gitignored.
- [ ] Compose file lives at repo root as `docker-compose.yml`.

## Implementation notes
- Use `pgvector/pgvector:pg16` for the database; mount `db/init.sql` into `/docker-entrypoint-initdb.d/` for automatic schema creation.
- LiteLLM can be pointed at the homelab service via an SSH tunnel or replaced with a mock for offline dev — document both.
- Orchestrator and ingestion should use the same `POSTGRES_DSN` the Helm secret will use in prod (keep env-var names identical to avoid drift).
- Bot service should be declared but `profiles: ["bot"]` so it only runs when explicitly requested (it needs a real Discord token).

## Dependencies
None.
