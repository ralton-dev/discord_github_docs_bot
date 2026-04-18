# Working Memory

Living doc. Update on every working session. Kept short on purpose — if a section grows past a page, promote it to a task file or an ADR.

**Last updated:** 2026-04-18
**Current phase:** 1 (MVP — ship to homelab)
**Wave 1 status:** ✅ tasks 01, 02, 03, 06 done. Wave 2 blocked on the registry decision (task 02 surfaced) before task 04 can run.

---

## Convention

- Each task lives in `implementation_plans/NN-slug.md` with YAML front matter (`status`, `phase`, `priority`).
- When a task is complete, `git mv` its file into `implementation_plans/done/` so the top level only shows open work.
- Task numbers never change, even after moves — they're identifiers, not ordering.
- This file is the source of truth for "what's happening right now"; individual task files are the source of truth for "how to do a specific thing".

---

## Current focus

Wave 1 shipped (compose, build/registry doc, Postgres provisioning scripts, unit-test harness). Next up: Wave 2 — task 04 (deploy first instance), 07 (integration tests), 08 (CI). Task 04 is blocked on the registry decision; 07 and 08 can proceed once 04 starts.

## In progress

_Nothing claimed yet._

## Recommended order

**Wave 1 — start in parallel from t=0 (no cross-deps):**
- **01 · Local dev compose** — tight local feedback loop.
- **02 · Image build + push** — registry images for helm to pull.
- **03 · Provision Postgres instance** — DB + role for the first instance.
- **06 · Unit tests** — pure-logic coverage; needs nothing.

**Wave 2 — unblocked once Wave 1 is mostly done:**
- **04 · Deploy first instance** — needs 02 + 03. Critical path.
- **07 · Integration tests** — needs 01 + 06.
- **08 · CI pipeline** — needs 02 + 06.

**Wave 3 — draft during/after 04:**
- **05 · Operational runbook** — best written as deploy pain is discovered, finalised once 04 works.

**Wave 4 — post-04, fully parallel streams:**
- **09 · Observability** — do this *first* in the RAG chain so later work has metrics to verify against.
- **10 · Secrets hardening** — mostly Helm chart, independent of service code.
- **15 · Webhook ingestion** — new endpoint, orthogonal to everything else.
- **16 · Thread conversations** — bot-side only, no orchestrator changes.

**Wave 5 — RAG retrieval-path chain (serialise; all touch `rag/app.py`):**
1. **11 · Hybrid search** — broadens recall.
2. **12 · Reranker** — filters candidates before the LLM.
3. **13 · Query cache** — short-circuits on repeat questions.
4. **14 · Rate limiting** — gates at the entry point.

## Dependency graph

```
        ┌── 01 local-compose ──┐
  t=0 ──┼── 02 images ─────────┼──> 04 deploy ──> 05 runbook
        ├── 03 postgres ───────┘
        └── 06 unit-tests ────────> 07 integration (needs 01)
                                 └> 08 CI (needs 02)

                                   ┌─ 09 observability
                                   ├─ 10 secrets
             after 04, fan out  ───┤  11 hybrid → 12 reranker → 13 cache → 14 rate-limit  (same file, serialise)
                                   ├─ 15 webhook ingest
                                   └─ 16 threads
```

**Hard blockers:** 04 ← (02, 03); 07 ← 01; 08 ← (02, 06); 09–16 all need 04 to validate.
**Soft deps:** 02 benefits from 01 (smoke-test images locally); 05 drafted during 04; 13 wants 09 first for cache metrics.
**Critical path to Phase 1 done:** `max(01, 02, 03)` → 04 → 05. Everything else parallelises around this spine.

## Blockers / open questions

- **Registry:** GHCR? Homelab Harbor? Local `registry:2`? Decides `images.*.repository` in values files.
- **Embedding model:** currently defaulted to `text-embedding-3-small` (1536 dim). Ollama `nomic-embed-text` (768) is cheaper and private — worth benchmarking recall before committing. Changing this later means re-embedding everything.
- **Postgres tenancy:** shared DB with `repo` column (current design) vs one DB per instance. Current scale suggests shared; revisit if a repo needs compliance isolation.
- **LiteLLM model aliases:** does the existing LiteLLM config expose `claude-opus-4-7` and `text-embedding-3-small` under those names, or do we need aliases? Check before first deploy.
- **First target repo:** which project are we pointing at first? That repo's size and language mix shape chunking defaults.

## Recent decisions

- **2026-04-18** Wave 1 complete (01 compose, 02 build/registry-docs, 03 Postgres provisioning scripts, 06 unit tests). 29/29 unit tests pass; `docker compose config` validates; `helm lint` clean.
- **2026-04-18** Image tag scheme = `<VERSION>-<short-sha>` (e.g. `0.1.0-9b83072`). Bump `VERSION` in Makefile for semver level changes.
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
