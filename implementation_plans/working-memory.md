# Working Memory

Living doc. Update on every working session. Kept short on purpose — if a section grows past a page, promote it to a task file or an ADR.

**Last updated:** 2026-04-18
**Current phase:** 1 (MVP — ship to homelab)

---

## Convention

- Each task lives in `implementation_plans/NN-slug.md` with YAML front matter (`status`, `phase`, `priority`).
- When a task is complete, `git mv` its file into `implementation_plans/done/` so the top level only shows open work.
- Task numbers never change, even after moves — they're identifiers, not ordering.
- This file is the source of truth for "what's happening right now"; individual task files are the source of truth for "how to do a specific thing".

---

## Current focus

Scaffold is complete (services, Dockerfiles, Helm chart, DB schema). Next up: Phase 1 — get one instance running end-to-end in the homelab cluster.

## In progress

_Nothing claimed yet._

## Next up (in order)

1. **01 · Local dev compose** — tight feedback loop before touching k8s.
2. **02 · Image build + push** — needed before helm install can pull anything.
3. **03 · Provision Postgres instance** — DB + role for the first bot instance.
4. **04 · Deploy first instance** — helm install + e2e smoke test.
5. **05 · Operational runbook** — capture what we learn deploying #04.

## Blockers / open questions

- **Registry:** GHCR? Homelab Harbor? Local `registry:2`? Decides `images.*.repository` in values files.
- **Embedding model:** currently defaulted to `text-embedding-3-small` (1536 dim). Ollama `nomic-embed-text` (768) is cheaper and private — worth benchmarking recall before committing. Changing this later means re-embedding everything.
- **Postgres tenancy:** shared DB with `repo` column (current design) vs one DB per instance. Current scale suggests shared; revisit if a repo needs compliance isolation.
- **LiteLLM model aliases:** does the existing LiteLLM config expose `claude-opus-4-7` and `text-embedding-3-small` under those names, or do we need aliases? Check before first deploy.
- **First target repo:** which project are we pointing at first? That repo's size and language mix shape chunking defaults.

## Recent decisions

- **2026-04-18** pgvector HNSW index (not IVFFlat) — no training step, good recall at homelab scale.
- **2026-04-18** OpenAI-compatible client against LiteLLM, not per-provider SDKs — single code path, LiteLLM handles routing.
- **2026-04-18** One Helm chart, one values file per repo, one namespace per instance — clean RBAC and per-instance secrets.
- **2026-04-18** Discord bot `replicas: 1` + `strategy: Recreate` — one gateway session per token; scale with Discord sharding, not pods.
- **2026-04-18** Ingestion clones into `emptyDir` each run, not a PVC — simpler, homelab-scale repos fit easily.
- **2026-04-18** Ingestion GCs old commits on success — storage stays bounded, reruns are idempotent.

## Parking lot (not promoted to tasks yet)

- NetworkPolicies to restrict rag → pgvector only.
- Backup/restore for the bot's Postgres database (likely reuses existing homelab postgres backups).
- Per-guild cost dashboard in Grafana once metrics land (task 09).
- Multi-turn conversations keyed on Discord thread ID (task 16, parked for phase 4).
