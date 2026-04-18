# Webhook-driven ingestion

By default, the ingestion CronJob runs every 6 hours. For actively developed
projects that's a staleness window of up to 6 hours before the bot knows about
a new commit. Enabling webhooks closes that gap: GitHub or GitLab posts to
the bot's `/webhook` endpoint on every push, and an ad-hoc ingestion Job
spawns within seconds. The cron stays as a safety net for cases where the
webhook is missed.

## Enabling webhooks on an instance

1. Generate a strong shared secret:
   ```sh
   openssl rand -base64 32 | tr -d '/+=' | head -c 40
   ```
2. In `values-<slug>.yaml`:
   ```yaml
   webhook:
     enabled: true
     stalenessThresholdSecs: 7200   # 2h default

   # Either add to the chart-managed Secret bootstrap path:
   secrets:
     webhookSecret: "<the-value>"
   # …or add WEBHOOK_SECRET to your sealed Secret (recommended long-term).
   ```
3. `make helm-install REPO=<slug>` — the chart renders the ServiceAccount,
   Role, and RoleBinding that let the rag pod create ingestion Jobs in its
   own namespace.
4. Expose the rag Service externally (NodePort, LoadBalancer, or an Ingress).
   The webhook endpoint is `POST <external-url>/webhook`.

## GitHub setup

Repo Settings → Webhooks → Add webhook:

| Field | Value |
|---|---|
| Payload URL | `https://<external-url>/webhook` |
| Content type | `application/json` |
| Secret | the `webhookSecret` from step 1 |
| Events | **Just the push event** |
| Active | ✅ |

GitHub signs the body with HMAC-SHA256 and sends `X-Hub-Signature-256`.

## GitLab setup

Project → Settings → Webhooks → Add new webhook:

| Field | Value |
|---|---|
| URL | `https://<external-url>/webhook` |
| Secret Token | the `webhookSecret` from step 1 |
| Trigger | **Push events** |

GitLab sends the token verbatim in `X-Gitlab-Token`.

## Checking ingestion freshness

```
GET /status/ingestion?repo=<slug>
```

Returns:
```json
{
  "last_success_at": "2026-04-18T09:12:43+00:00",
  "seconds_since_last_success": 142,
  "status": "ok"   // or "stale" / "unknown"
}
```

Wire this into a monitoring dashboard or an alert when `status != "ok"`.

## Troubleshooting

- **401 `signature mismatch`** — wrong secret, body tampered in transit
  (check proxy), or wrong provider header. Compare signature computation
  manually: `echo -n '<body>' | openssl dgst -sha256 -hmac '<secret>'`.
- **401 `WEBHOOK_SECRET is not configured`** — the rag pod has an empty
  secret. Check the Secret has `WEBHOOK_SECRET` set, then restart the
  rag Deployment so the new env is picked up.
- **429 rate-limited** — one ingestion per repo per 60s. Legitimate if
  pushes are frequent; rapid `push --force-with-lease` cycles can trip it.
  Retry after the returned `retry_after` seconds.
- **500 `webhook misconfigured`** — `INGEST_CRONJOB_NAME` or
  `POD_NAMESPACE` is empty. These come from the chart when
  `webhook.enabled=true`; check `kubectl exec <rag-pod> env | grep -i
  ingest` to confirm.
- **500 `create_namespaced_job failed`** — RBAC issue. Check the Role
  and RoleBinding are present and reference the rag ServiceAccount.

## Known limits

- The rate-limiter is **in-process**. With `ragOrchestrator.replicas=2`, two
  pods each allow one call per 60s → two bursts worst-case. Acceptable for
  homelab scale; replace with a Redis-backed or Kubernetes-lease-based
  limiter if you scale horizontally past ~4 replicas.
- Rotation: to rotate `webhookSecret`, update the Secret (sealed or plain),
  restart the rag Deployment, then update the webhook on GitHub/GitLab to
  the new value. There is a brief window where the old secret is rejected
  before the new one is registered; schedule the rotation during a push
  lull.
