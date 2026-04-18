---
status: todo
phase: 1
priority: high
---

# 05 · Operational runbook

## Goal
Write a runbook covering the everyday operations of running GitDoc instances: adding a new repo, rolling out a new image, rotating secrets, and debugging the most likely failure modes.

## Why
"How do I add a new project?" should have a 10-minute answer. Without this, every new instance re-learns the deploy flow from scratch.

## Acceptance criteria
- [ ] `docs/runbook.md` (or equivalent) exists and covers:
  - [ ] Adding a new instance: provision Postgres, craft values file, `helm install`, trigger ingestion, verify.
  - [ ] Rolling out a new image version across all instances.
  - [ ] Rotating Discord token, LiteLLM key, Postgres password.
  - [ ] Force-reingesting a repo (what to delete, how to trigger).
  - [ ] Debugging: bot not responding, ingestion failing, answers ungrounded, rate-limited by LLM.
- [ ] Each procedure validated by walking through it once on task 04's instance.

## Implementation notes
- Every time a deploy procedure surprises us during task 04, capture it here.
- Prefer copy-pasteable `kubectl` one-liners over prose.
- Include the values files layout: which fields change per instance, which stay in chart defaults.

## Dependencies
- 04 (needs a real deployment to validate procedures against)

---

## Appendix: Required CI checks (drafted from task 08)

> Drafted while wiring up `.github/workflows/ci.yml`. When task 05 writes
> the full runbook, fold this into the "Day-2 ops" section.

GitHub repo settings → **Branches** → branch protection rule on `main`:

- Require a pull request before merging.
- Require status checks to pass before merging.
- Required status checks (exact names that appear in the Actions UI):
  - `Lint + unit tests` (job `lint-and-unit-test` in `ci.yml`).
  - `Build images (verification, no push) (discord-bot)` (matrix leg).
  - `Build images (verification, no push) (rag)` (matrix leg).
  - `Build images (verification, no push) (ingestion)` (matrix leg).
  - `Integration tests` — **add this once plan 07 lands**. The job is
    currently gated on `hashFiles('tests/integration/**')` and is skipped
    (does not produce a check run) until that tree exists.
- Require branches to be up to date before merging (recommended).
- Restrict who can push to matching branches (admin override allowed if
  you're the only operator, but disable force-push).

Release workflow notes:

- `release.yml` triggers on tag pushes matching `v*` and uses
  `secrets.GITHUB_TOKEN` (no manual setup) to push to
  `ghcr.io/ralton-dev/gitdoc-<service>`.
- After the first successful publish, set the package visibility to
  **Public** in GitHub Packages → each `gitdoc-<service>` package →
  Settings, so the chart can pull without a `docker-registry` secret
  (matches `images.*.repository` defaults in the chart values).

---

## Appendix: Drafted from task 04

> Captured while preparing `deploy/DEPLOY.md`, `scripts/smoke-test.sh`, and
> the per-instance values template. These are items the runbook MUST cover
> — they surfaced as real footguns / repeated questions while making the
> chart deploy-ready, but writing the full procedures belongs here in 05.

- **Image-tag rollout across all instances.** For "deploy a new version
  everywhere": loop over each `values-<slug>.yaml`, bump
  `images.{bot,rag,ingestion}.tag` to a single new value, then
  `helm upgrade --install` each release. Document the
  `make print-tag` workflow + `helm get values <release> -n <ns>` for
  finding the currently-deployed tag. Decide whether tag bumps go through
  PR review (since values files are gitignored, a process question rather
  than a tooling one).
- **Per-instance values file lifecycle.** Values files contain plaintext
  Discord token + LiteLLM key + Postgres DSN. They are gitignored. Decide
  and document the storage location (1Password vault entry per instance?
  homelab-ops repo encrypted with sops? local-only on the operator
  workstation?) and the handover procedure when ownership of an instance
  changes hands. This block becomes obsolete when plan 10 lands sealed-
  secrets / external-secrets.
- **Force-reingest semantics.** Ingestion GC's old commits on success and
  uses `ON CONFLICT DO NOTHING`, so a manual rerun on the same SHA is a
  no-op. To truly re-embed (e.g. after changing chunking params, or after
  detecting bad embeddings) the operator must EITHER delete rows from
  `chunks` for the repo first OR change the embedding model (which forces
  a full re-ingest because of the dim mismatch). Document both routes
  with copy-paste SQL.
- **Manual ingestion trigger one-liner.**
  `kubectl -n gitdoc-<slug> create job --from=cronjob/gitdoc-<slug>-ingest
  gitdoc-<slug>-ingest-manual-$(date +%s)`. Already encoded in
  `scripts/smoke-test.sh`; lift it out for the runbook.
- **Secret rotation procedures, one per secret.** Discord token: regen
  in dev portal → edit values file → `helm upgrade` → bot pod restarts
  (the `Recreate` strategy means a brief gateway gap, mention it).
  LiteLLM key: same shape. Postgres password: `ALTER ROLE gitdoc_<slug>
  PASSWORD '...';` then update DSN in values file then `helm upgrade`.
- **`/readyz` is the early signal for DB issues.** Document the
  one-liner debugging recipe from `DEPLOY.md#troubleshooting`:
  `kubectl exec deploy/gitdoc-<slug>-rag -- python -c "import os, psycopg;
  psycopg.connect(os.environ['POSTGRES_DSN'], connect_timeout=5)"` —
  surfaces the real driver error instead of just "503 db unavailable".
- **LiteLLM alias verification.** "Hit /v1/models from a debug pod" should
  be a runbook one-liner — most common cause of /ask 502s and is
  invisible from k8s logs without it.
- **`embedDim` mismatch is a footgun.** The `chunks.embedding` column
  type is fixed at install time. Document the rule: pick the embedding
  model up front, never change it on a live instance. To switch you must
  drop the database and re-ingest.
- **Decommission checklist beyond `helm uninstall`.** `DEPLOY.md` covers
  the K8s + Postgres side. The runbook should add: revoke the bot from
  the Discord guild, rotate the bot token if shutting down for security
  reasons, prune images from GHCR if the project is dead.
- **Smoke test as ad-hoc health check.** `scripts/smoke-test.sh` is also
  useful for "is this instance still healthy?" outside deploy day. Mention
  it in the troubleshooting tree as the first thing to run before deeper
  digging.
- **NetworkPolicy guidance (placeholder until plan 10 ships).** Once
  egress restrictions land, "/ask suddenly stopped working after an
  unrelated change" → suspect NetworkPolicy on the rag service
  egress rules. Capture the diagnostic recipe alongside the policy itself.
- **Validate every procedure on a real homelab instance.** The plan-05
  acceptance criterion `Each procedure validated by walking through it
  once on task 04's instance` is the gate — do not ship the runbook
  prose without the walk-through.
