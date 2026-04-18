# Working Memory

Living doc. Update on every working session. Kept short on purpose — if a section grows past a page, promote it to a task file or an ADR.

**Last updated:** 2026-04-18
**Current phase:** all coded phases shipped. Only plan 05 (runbook) remains — intentionally deferred until post-deploy.
**Waves done:** 1 (01, 02, 03, 06), 2 (04, 07, 08), 4a (10, 15, 16), 4b (09, 17), 5 (11, 12, 13, 14). 16 of 17 plans complete; 05 open by design.
**Tests:** 165 unit + 8 integration, all green.
**Operator-blocking before first live deploy:** push a `v0.1.0` tag (CI publishes images to GHCR), mark GHCR packages Public, provision Postgres via `db/provision/`, fill `values-<slug>.yaml`, `make helm-install`, run `scripts/smoke-test.sh`. All code, docs, chart, and CI are in place.

---

## Convention

- Each task lives in `implementation_plans/NN-slug.md` with YAML front matter (`status`, `phase`, `priority`).
- When a task is complete, `git mv` its file into `implementation_plans/done/` so the top level only shows open work.
- Task numbers never change, even after moves — they're identifiers, not ordering.
- This file is the source of truth for "what's happening right now"; individual task files are the source of truth for "how to do a specific thing".

---

## Current focus

All waves shipped. Plan 05 (operational runbook) is the only open item — deferred by design until the operator has walked through a real deploy so the procedures are battle-tested. The runbook file already has eight appendices (from tasks 04, 08, 09, 10, 11, 12, 13, 14, 16, 17) capturing what it must cover.

**Next action is on the operator:** push `v0.1.0`, deploy, run the smoke test, verify `/ask` from Discord. Then plan 05 gets written based on what surprised us in practice.

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

- **First live deploy:** unknown until the operator runs through `deploy/DEPLOY.md` against the homelab. The procedure is captured but unverified end-to-end.
- **GHCR package visibility:** must be set to **Public** after the first successful publish (Settings → Packages → each `gitdoc-*`). Otherwise the chart's `pullPolicy: IfNotPresent` will silently fail without a `regcred` secret.
- **LiteLLM model aliases:** confirm `text-embedding-3-small` and `ollama_chat/llama3.2:3b` are exposed under those names in the homelab LiteLLM. The chart values let you override per-instance if not.
- **First target repo:** the per-instance values file decides — flagged for the operator at deploy time, not a code blocker.
- **Postgres tenancy:** shared DB with `repo` column (current design) vs one DB per instance. Current scale suggests shared; revisit if a repo needs compliance isolation.

## Recent decisions

- **2026-04-18** Wave 5 complete. Hybrid search (BM25 + vector via RRF k=60, generated `content_tsv` column), cross-encoder reranker (off by default, pluggable HTTP shape, graceful-degrade on failure), query cache keyed on `(repo, commit_sha, query_hash)` with natural invalidation on new ingestion SHA, per-guild/per-user token rate limits with friendly 429 surfaced by the bot. All feature-flagged so operators can A/B or kill individual waves. 165 unit + 8 integration tests.
- **2026-04-18** Service `rag-orchestrator` renamed to `rag` everywhere. Image is now `ghcr.io/ralton-dev/gitdoc-rag` (resolves the latent Makefile/chart name mismatch).
- **2026-04-18** Registry = GHCR (`ghcr.io/ralton-dev`), public packages, no `regcred`. CI publishes via tag push using `GITHUB_TOKEN`.
- **2026-04-18** Default chat model = `ollama_chat/llama3.2:3b`, runtime-overridable once plan 17 ships. Embedding stays `text-embedding-3-small` (changing it later invalidates every stored vector).
- **2026-04-18** Plan 17 added (Wave 4): per-instance chat model in DB, `/model list/current/set` slash commands, gated by Discord's built-in `Manage Server` permission. No custom roles or user allowlists for now.
- **2026-04-18** Wave 4a complete (10 sealed-secrets + existingSecret gate, 15 /webhook + /status/ingestion + RBAC + docs, 16 thread-aware conversations with graceful 422 fallback). 58 unit + 4 integration tests pass; helm lint + template clean in all three modes (default / existingSecret / webhook.enabled).
- **2026-04-18** Secrets flow = **sealed-secrets** (plan 10). Steady-state: operator seals a plain Secret with `kubeseal`, commits sealed manifest under `deploy/sealed-secrets/`, points `secrets.existingSecret` at its name. Bootstrap path (plaintext via `secrets.*`) kept for first-deploy-before-sealed-secrets-installed.
- **2026-04-18** Bot requires **Message Content Intent** privileged (enabled in code; operator must toggle in Discord Developer Portal) — needed for plan 16 thread follow-ups.
- **2026-04-18** Wave 2 complete (04 deploy package + chart hardening, 07 integration tests with testcontainers, 08 CI/release workflows). 29 unit + 4 integration tests pass; `helm lint`/`template`/`compose config` clean. **Cluster-side steps (push tag, provision Postgres, helm install, smoke test) remain on the operator.**
- **2026-04-18** Wave 1 complete (01 compose, 02 build/registry-docs, 03 Postgres provisioning scripts, 06 unit tests).
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
