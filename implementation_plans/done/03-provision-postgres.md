---
status: todo
phase: 1
priority: high
---

# 03 · Provision Postgres instance

## Goal
Create a dedicated DB and role in the existing homelab Postgres for the first bot instance, and document the procedure so new instances can be provisioned quickly.

## Why
The Helm `db-migrate` hook creates tables but expects the DB and role to already exist. Provisioning is a one-time per-instance admin step.

## Acceptance criteria
- [ ] A `gitdoc_<slug>` database exists on the homelab Postgres.
- [ ] A `gitdoc_<slug>` role exists with `CONNECT`, `CREATE`, and table privileges scoped to that database only.
- [ ] `pgvector` extension is available on that database (the `db-migrate` Job will issue `CREATE EXTENSION IF NOT EXISTS vector`, which needs superuser on some Postgres builds — verify once).
- [ ] A `POSTGRES_DSN` for that role is securely stored and referenced from the instance's values file.
- [ ] Procedure documented in the operational runbook (task 05) so step 1–4 can be repeated.

## Implementation notes
- `pgvector` extension install typically needs to be done once by a superuser per database. On cloud Postgres it's allowlisted; on self-hosted it's usually fine.
- Role password ideally managed via whatever secrets flow task 10 picks. For MVP, a manually generated password dropped into `values-<slug>.yaml` is acceptable.
- Keep role naming predictable: `gitdoc_<slug>` for the role, `gitdoc_<slug>` for the DB, matching the helm release slug.

## Dependencies
None (pre-req for task 04).
