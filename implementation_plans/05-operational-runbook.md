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
