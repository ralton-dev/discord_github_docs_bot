---
status: todo
phase: 2
priority: medium
---

# 10 · Secrets hardening

## Goal
Replace plain-text values-file secrets with a managed secrets flow (sealed-secrets or external-secrets), so committed values files contain no credentials.

## Why
Right now, `values-<slug>.yaml` contains the Discord token and DB password. That's fine for the homelab bootstrap but unacceptable long term — any accidental commit leaks production creds.

## Acceptance criteria
- [ ] Either sealed-secrets or external-secrets selected and documented (decision noted in working memory).
- [ ] Helm chart updated: `secrets.*` values are optional; when an `existingSecret` name is supplied, the Secret template is skipped.
- [ ] Per-instance values files stop containing raw credentials.
- [ ] `.gitignore` already excludes `values-*.yaml` (except `values-example.yaml`); confirm.
- [ ] Runbook (task 05) updated with the new rotation procedure.

## Implementation notes
- Sealed-secrets is simpler for a homelab: encrypt once, commit the sealed file, kubeseal does the rest.
- External-secrets is better if there's a Vault or cloud secrets manager to pull from.
- Helm chart change: add `secrets.existingSecret: ""` to values; conditionally render `secret.yaml`.

## Dependencies
- 04 (need a working deploy before rearchitecting the secret flow)
