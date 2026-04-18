---
status: todo
phase: 1
priority: high
---

# 04 · Deploy first instance + e2e smoke test

## Goal
Run `helm upgrade --install` for the first real repo and verify end-to-end: ingestion populates the DB, the bot responds to `/ask` on Discord with a grounded answer plus citations.

## Why
This is the first proof that the architecture works against real infrastructure. Everything up to here is scaffolding.

## Acceptance criteria
- [ ] `make helm-install REPO=<slug>` succeeds, all pods reach Ready.
- [ ] `db-migrate` hook completes without errors.
- [ ] First scheduled (or manually triggered) ingestion run inserts chunks into `chunks` and writes a row to `ingest_runs` with `status='ok'`.
- [ ] A real `/ask` invocation on Discord returns an answer and includes at least one citation.
- [ ] Answer quality sanity-checked: ask three questions you know the answers to, verify groundedness.
- [ ] Anything the runbook (task 05) needs to cover is logged as it's discovered.

## Implementation notes
- Trigger ingestion manually first instead of waiting for the schedule: `kubectl create job --from=cronjob/<name>-ingest <name>-ingest-manual -n gitdoc-<slug>`.
- Tail the orchestrator logs during first `/ask` — if it can't reach LiteLLM or Postgres, errors surface there.
- If the embedding dimension mismatches the column type, ingestion errors immediately — fix `models.embedDim` before re-running.
- Use the smallest value of `top_k` that still gives grounded answers; higher values burn tokens.

## Dependencies
- 02 (images in registry)
- 03 (DB/role exists)
