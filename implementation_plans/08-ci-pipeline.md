---
status: todo
phase: 2
priority: medium
---

# 08 · CI pipeline

## Goal
GitHub Actions workflow that on every push lints, runs unit tests, and on tagged releases builds & pushes all three images to the chosen registry.

## Why
Removes human steps from the release path and catches breakage before it reaches the homelab.

## Acceptance criteria
- [ ] `.github/workflows/ci.yml` runs on PRs: `helm lint`, `helm template` (catch templating errors), `pytest`.
- [ ] `.github/workflows/release.yml` runs on `v*` tag pushes: builds and pushes all three images tagged with both `vX.Y.Z` and the git SHA.
- [ ] Failing tests or lint block PR merges (branch protection rule noted in runbook).
- [ ] Builds cached between runs (layer cache or buildx cache) so release CI doesn't take > 5 min.

## Implementation notes
- Use `docker/build-push-action` with `cache-from: type=gha, cache-to: type=gha` for layer caching.
- If GHCR is the registry, `GITHUB_TOKEN` already has push permission — no extra secrets needed.
- Keep unit tests (fast, parallel) separate from integration tests (slower, serial) in the workflow.

## Dependencies
- 02 (registry chosen)
- 06 (unit tests exist)
