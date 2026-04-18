---
status: todo
phase: 1
priority: high
---

# 02 · Image build + push

## Goal
Publish the three container images (`gitdoc-discord-bot`, `gitdoc-rag`, `gitdoc-ingestion`) to a registry the homelab cluster can pull from.

## Why
`helm install` cannot succeed until pullable images exist. This task unblocks the first real deploy.

## Acceptance criteria
- [ ] Registry chosen and documented in working memory (GHCR, Harbor, or local `registry:2`).
- [ ] All three images build cleanly on amd64 and match the homelab node architecture.
- [ ] `make build REGISTRY=<chosen>` and `make push REGISTRY=<chosen>` work end-to-end from a clean checkout.
- [ ] Images tagged with a semver + git SHA (e.g. `0.1.0-ab12cd3`) so rollouts are traceable.
- [ ] `values.yaml` defaults updated to point at the chosen registry.
- [ ] If the registry requires auth, a `regcred` Secret is documented (not committed).

## Implementation notes
- The existing `Makefile` already iterates over `SERVICES`; extend it to also stamp a git SHA tag if that's wanted.
- Multi-arch builds (amd64+arm64) are cheap via `docker buildx` if the homelab has mixed nodes — worth doing preemptively.
- Verify image sizes stay reasonable (< 300MB). The ingestion image will be biggest due to git.

## Dependencies
None (but task 01 makes local verification easier before pushing).
