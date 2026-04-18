---
status: todo
phase: 4
priority: low
---

# 15 · Webhook-driven ingestion

## Goal
Add an HTTP endpoint that accepts GitHub/GitLab push webhooks and triggers an ingestion run immediately, so the knowledge base is fresh within seconds of a commit rather than waiting for the next cron firing.

## Why
The cron schedule (every 6h by default) creates a stale window. For actively developed projects, hour-old answers can mislead. Webhook ingestion closes that gap.

## Acceptance criteria
- [ ] New `ingestion-trigger` service (or endpoint on orchestrator) accepts signed webhooks.
- [ ] Webhook signature verified using a per-instance shared secret.
- [ ] On valid webhook, kicks off a Kubernetes Job (via the k8s API) from the CronJob template.
- [ ] Bot status / orchestrator metric exposes "seconds since last successful ingestion" so staleness is visible.
- [ ] Existing CronJob stays as a safety net (catches cases where webhooks fail).

## Implementation notes
- Easiest path: add a `/webhook` endpoint to the RAG orchestrator and use the Kubernetes Python client to create Jobs. That avoids a new service.
- Alternative: Argo Events, but it's heavy for a homelab.
- Rate-limit the webhook endpoint (one ingestion per repo per 60s) to prevent storms.

## Dependencies
- 04 (baseline working)
