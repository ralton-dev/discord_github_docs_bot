---
status: done
phase: 1
priority: high
---

# 05 · Operational runbook

Day-2 ops for a deployed gitdoc instance. Scanning order: the thing you
want to do, the copy-pasteable commands, then the debugging tree when
something's broken.

Substitute throughout:

- `<slug>` — the instance slug (e.g. `mylab`)
- `<ns>` — the namespace, which is always `gitdoc-<slug>`
- `<super_dsn>` — superuser DSN to the shared homelab Postgres
  (you have this; the instance roles do not)

---

## Quick reference

```sh
# Is this instance healthy?
NAMESPACE=<ns> RELEASE=gitdoc-<slug> ./scripts/smoke-test.sh

# Tail the full interaction flow for one /ask
# (Grafana LogQL — same filter works in kubectl too with | grep)
{namespace="<ns>"} | json | event=~"bot.ask|ask.received|ask.completed|bot.response"

# Pods, events, the usual
kubectl -n <ns> get pods,cronjob,job

# Force an immediate ingestion (e.g. after changing models.embed on a fresh DB)
kubectl -n <ns> create job --from=cronjob/gitdoc-<slug>-ingest \
  gitdoc-<slug>-ingest-manual-$(date +%s)

# Force a full re-embed on the same SHA (bumps in chunking rules, etc.)
# default CronJob skips re-embed when SHA hasn't moved; this overrides
kubectl -n <ns> create job --from=cronjob/gitdoc-<slug>-ingest \
  gitdoc-<slug>-reingest-$(date +%s) --dry-run=client -o yaml \
  | yq '.spec.template.spec.containers[0].env += [{"name":"FORCE_REINGEST","value":"true"}]' \
  | kubectl apply -f -
```

---

## Add a new instance

Start-to-finish, assume you already have:

- A Discord application + bot token (if not, see
  [`docs/discord-setup.md`](../docs/discord-setup.md))
- sealed-secrets controller installed (one-time per cluster)
- ArgoCD deployed and pointing at your homelab-ops repo

**1. Generate per-instance Postgres credentials**

```sh
export SLUG=<slug>
export INSTANCE_PW=$(openssl rand -base64 32 | tr -d '/+=' | head -c 40)
```

**2. Provision the Postgres role + database**

Runs in-cluster, no workstation psql needed. Superuser creds come from
wherever you keep them (env vars in the snippet below):

```sh
kubectl run gitdoc-$SLUG-provision --rm -i --restart=Never \
  --image=postgres:16-alpine \
  --env "PGUSER=$SUPER_USER" --env "PGPASSWORD=$SUPER_PW" \
  --env "PGHOST=$PG_HOST" --env "PGPORT=$PG_PORT" --env "PGDATABASE=postgres" \
  -- sh -c "psql -v ON_ERROR_STOP=1 -v slug=$SLUG -v password='$INSTANCE_PW' -f -" \
  < <(curl -sSL https://raw.githubusercontent.com/ralton-dev/discord_github_docs_bot/main/db/provision/provision-instance.sql)
```

**3. Seal the Secret**

Build the plain Secret locally, pipe through `kubeseal`, commit the
sealed manifest to your homelab-ops repo (NOT this repo):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: gitdoc-<slug>
  namespace: gitdoc-<slug>
type: Opaque
stringData:
  DISCORD_BOT_TOKEN: "<from Discord dev portal>"
  LITELLM_API_KEY:   "<litellm master or virtual key>"
  POSTGRES_DSN:      "postgresql://gitdoc_<slug>:<INSTANCE_PW>@<pg-host>:5432/gitdoc_<slug>?sslmode=require"
  GIT_TOKEN:         ""   # fill if target repo is private
  WEBHOOK_SECRET:    ""   # fill iff you enable webhook.enabled
```

**4. ArgoCD Application**

Pin `targetRevision` to the release tag you want. Point at a
per-instance values file in your homelab-ops repo — NEVER commit values
files to the public gitdoc repo (they carry the `existingSecret` name
you may not want to leak).

```yaml
spec:
  source:
    repoURL: https://github.com/ralton-dev/discord_github_docs_bot.git
    targetRevision: v0.0.10
    path: deploy/helm/gitdoc
    helm:
      values: |
        repo:
          name:  "<slug>"
          url:   "<target-repo-https-url>"
          branch: "main"   # or master / develop / whatever
        images:
          bot:       { tag: "v0.0.10" }
          rag:       { tag: "v0.0.10" }
          ingestion: { tag: "v0.0.10" }
        secrets:
          existingSecret: "gitdoc-<slug>"
        discordBot:
          guildAllowlist: "<discord-guild-id>"
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
```

**5. Smoke test**

```sh
NAMESPACE=<ns> RELEASE=gitdoc-<slug> ./scripts/smoke-test.sh
```

**6. Discord dev-portal steps**

- Enable **MESSAGE CONTENT INTENT** (required for thread follow-ups).
- Make the bot **Public Bot: OFF** if you haven't (prevents strangers
  inviting it).
- Paste the OAuth2 invite URL from `docs/discord-setup.md` and add the
  bot to your server.
- Right-click server → **Copy Server ID** → already in
  `discordBot.guildAllowlist` above.

**7. Pick a chat model**

The instance refuses `/ask` until one is set:

```
/model list               # see what LiteLLM exposes
/model set ollama_chat/llama3.2:3b   # or any id from the list
```

Requires Discord's **Manage Server** permission on the caller.

---

## Roll a new image across all instances

CI publishes on tag push. Two parts: tag the release, bump each
ArgoCD Application.

```sh
# in this repo
make bump-version VERSION=x.y.z
git push origin main --follow-tags   # triggers release.yml; images land on GHCR
```

Then in your homelab-ops repo, bump the `targetRevision` AND
`images.*.tag` to `vX.Y.Z` for every ArgoCD Application. One commit per
instance, or one sweeping commit if you prefer atomic cluster rolls.
ArgoCD picks up the change and syncs each release.

Confirm each instance is on the new tag:

```sh
kubectl get pod -A -l app.kubernetes.io/name=gitdoc \
  -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,IMAGE:.spec.containers[0].image \
  | grep gitdoc-rag
```

---

## Secret rotation

Each secret has a specific procedure. All four+ Secret keys live in the
same sealed Secret.

### Discord bot token

1. Discord dev portal → Bot → **Reset Token** → copy
2. Re-seal the Secret with the new `DISCORD_BOT_TOKEN` value and apply
3. `kubectl -n <ns> rollout restart deploy/gitdoc-<slug>-bot`

Brief gap while the pod reconnects (new Session ID appears in logs).
No Discord re-invite needed; the application identity is unchanged.

### LiteLLM API key

1. Rotate in LiteLLM config (new virtual key / master key)
2. Re-seal the Secret with the new `LITELLM_API_KEY`
3. `kubectl -n <ns> rollout restart deploy/gitdoc-<slug>-rag`
   (ingestion will pick it up on next CronJob run)

### Postgres password

```sql
ALTER ROLE gitdoc_<slug> PASSWORD '<new-password>';
```

Then update the DSN inside the sealed Secret (the `POSTGRES_DSN` key),
re-seal, re-apply. Restart both deployments:

```sh
kubectl -n <ns> rollout restart deploy/gitdoc-<slug>-bot deploy/gitdoc-<slug>-rag
```

Ingestion picks up the new password on its next scheduled run.

### GitHub PAT (`GIT_TOKEN`)

1. Revoke the old PAT on GitHub
2. Create a fresh fine-grained PAT, read-only on the single target repo
3. Re-seal + re-apply
4. Next ingestion Job uses the new token automatically

If the token ever leaked (see [post-incident learnings](#post-incident-learnings),
v0.0.4), the revoke step is what actually mitigates; everything else is
cleanup.

### Webhook shared secret

1. Gen a new value
2. Update the Secret
3. `kubectl -n <ns> rollout restart deploy/gitdoc-<slug>-rag`
4. Update the webhook config on GitHub/GitLab with the new value

Brief rejection window while the pod restarts; schedule during a push
lull.

---

## Change the active chat model

Runtime, no redeploy. From Discord, as a user with **Manage Server**:

```
/model list                    # what LiteLLM exposes right now
/model set <model-id>          # persist to instance_settings
/model current                 # confirm + see updated_at / updated_by
```

Takes effect on the NEXT `/ask` — up to 15s for the per-repo cache to
expire. If urgent: `kubectl -n <ns> rollout restart deploy/gitdoc-<slug>-rag`
to force-flush.

Embedding model is different — it's `models.embed` in the chart and
**requires a full re-ingest if changed** (the vectors in `chunks` are
dimensionally tied to the embedding model). Don't change on a populated DB.

---

## Force a full re-embed

Default CronJob behaviour (v0.0.6+): if the branch SHA hasn't moved
since the last `ok` ingestion, skip the embed work and exit. You want
to override this when:

- Bumping chunking rules in `services/ingestion/ingest.py`
- Swapping `models.embed` on a fresh DB
- Recovering from a corrupt run

```sh
kubectl -n <ns> create job --from=cronjob/gitdoc-<slug>-ingest \
  gitdoc-<slug>-reingest-$(date +%s) --dry-run=client -o yaml \
  | yq '.spec.template.spec.containers[0].env += [{"name":"FORCE_REINGEST","value":"true"}]' \
  | kubectl apply -f -
```

Watch the logs:

```sh
kubectl -n <ns> logs job/gitdoc-<slug>-reingest-<epoch> -f
```

Confirm completion:

```
{"event": "ingest.complete", "repo": "<slug>", "sha": "...", "chunks": N, "deleted": N}
```

`deleted` will be non-zero on a real re-embed because old chunks for
the same SHA get GC'd and replaced.

---

## Decommission an instance

```sh
# Discord side
# 1. Kick the bot from the guild (Server Settings → Integrations)
# 2. Dev portal → Delete App (optional — frees up the app name)

# Cluster side
# 3. Remove the ArgoCD Application — ArgoCD prunes the workloads
# 4. Drop the Postgres role and database
kubectl run gitdoc-<slug>-revoke --rm -i --restart=Never \
  --image=postgres:16-alpine \
  --env "PGUSER=$SUPER_USER" --env "PGPASSWORD=$SUPER_PW" \
  --env "PGHOST=$PG_HOST" --env "PGPORT=$PG_PORT" --env "PGDATABASE=postgres" \
  -- sh -c "psql -v ON_ERROR_STOP=1 -v slug=<slug> -f -" \
  < <(curl -sSL https://raw.githubusercontent.com/ralton-dev/discord_github_docs_bot/main/db/provision/revoke-instance.sql)

# 5. Delete the sealed Secret manifest from the homelab-ops repo
# 6. (Optional) prune GHCR images if the project is permanently dead
```

**Destructive.** `DROP DATABASE` takes every chunk / cache / setting
with it. Snapshot beforehand if you might want the embeddings back.

---

## Debugging, by symptom

### Bot shows **offline** in Discord

- `kubectl -n <ns> logs deploy/gitdoc-<slug>-bot --tail=50`
- Happy path: `bot.ready` event with `logged in as <name>#<disc>`
- `401 Unauthorized` → token wrong in the Secret. Regen, re-seal, restart.
- `TypeError: intents.message_content` / Discord 4014 disconnect → **MESSAGE CONTENT INTENT** not toggled in the dev portal.
- Pod CrashLoopBackOff → `kubectl -n <ns> describe pod ...` and look for image pull / env var errors.

### Bot online but `/ask` autocomplete doesn't show

- Slash commands register on `tree.sync()` during bot startup — can take up to a minute on first boot.
- If it's been minutes and they still don't appear, the invite URL likely missed the `applications.commands` scope. Re-invite with the URL from `docs/discord-setup.md`.

### `/ask` replies "This bot doesn't have a chat model configured yet"

Expected on a fresh instance. A Manage-Server user runs
`/model set <name>`. See [Change the active chat model](#change-the-active-chat-model).

### `/ask` hangs then replies with "Something went wrong"

Bot hit its 60–180s timeout waiting for rag. `kubectl -n <ns> logs
deploy/gitdoc-<slug>-rag | jq -r 'select(.event=="ask.completed")'` —
the rag's `latency_ms` will tell you what actually happened.

Common causes:

- **Cold Ollama model.** First call after pod restart is slow; retry
  usually works. Bump `discordBot.askTimeoutSecs` if persistent.
- **LiteLLM unreachable.** Curl from a debug pod:
  `kubectl -n <ns> exec deploy/gitdoc-<slug>-rag -- python -c "import
  urllib.request; print(urllib.request.urlopen('$LITELLM_BASE_URL/v1/models').read())"`
- **Wrong chat model alias.** `/model list` reveals what LiteLLM
  actually exposes; `/model set` to a valid one.

### `/ask` answers but no thread opens

- v0.0.10+ logs `event=bot.thread_skipped` with a hint field.
- **Most common:** bot lacks `Create Public Threads` and/or
  `Read Message History` on that channel. In Discord: channel Edit →
  Permissions → **"View permissions as <bot user>"** — shows effective
  perms after all the override layers. Fix the deny at whichever layer
  is winning (`@everyone` on the category is the usual culprit).
- Before v0.0.10, these failures were ERROR-level — misleading. v0.0.10
  is WARNING; if you're stuck below that, see the release notes.

### Reply inside a thread is ignored

- Bot's `on_message` only fires when `message.channel` is a `Thread`
  whose starter message is the bot's.
- If `message_content` intent isn't active (dev portal toggle), the bot
  sees an empty `.content` string — appears to ignore. Re-check the
  portal.
- Log filter to confirm:
  `{namespace="<ns>"} | json | event="bot.thread_followup"`
  If you see the event but the bot doesn't respond, look at the next
  log line — probably a 422 history fallback or a rate limit.

### `/ask` returns 429 "You're asking too fast"

Hit a token budget:

```logql
{namespace="<ns>"} | json | event="ask.rate_limited"
```

Carries `reason` (`guild_budget` / `user_budget`) and `retry_after`. If
the cap is too tight, bump `rateLimits.guildTokensPerHour` or
`rateLimits.userTokensPerHour` in the Application's values.

### Ingestion job failed

```sh
kubectl -n <ns> get jobs -l app.kubernetes.io/component=ingestion --sort-by=.metadata.creationTimestamp
kubectl -n <ns> logs job/<the-failed-one>
```

Common modes:

- **Clone failed (fatal: auth)** → `GIT_TOKEN` wrong or missing for a
  private repo. Regen PAT, re-seal.
- **`fatal: repository ... not found`** → `repo.url` or `repo.branch`
  wrong. Check values.
- **Embedding call failures** → LiteLLM unreachable, see above.
- **`DELETE FROM chunks`** taking forever → very large repo after a
  branch rebase. Let it finish; subsequent runs will be fast.

### Ingestion scheduled but nothing happens

```sh
kubectl -n <ns> get cronjob   # check SCHEDULE + LAST SCHEDULE
```

- CronJob suspended? `kubectl -n <ns> patch cronjob gitdoc-<slug>-ingest -p '{"spec":{"suspend":false}}'`
- Last run skipped intentionally (v0.0.6+)? Logs will show
  `event=ingest.skipped` with the SHA. Expected when the branch hasn't
  moved.
- Job fired but exited immediately? Check Job status + events.

### Answers are outdated (cache not invalidating)

- `query_cache` is keyed on `(repo, commit_sha, query_hash)`. New
  commit_sha invalidates every cached answer automatically (ingestion
  handles the DELETE).
- To flush all cached answers right now, exec into the rag pod and run
  `DELETE FROM query_cache WHERE repo='<slug>'`.
- Or just disable while debugging: set `queryCache.enabled=false` and
  sync.

### DB connection errors in rag / bot

```sh
kubectl -n <ns> exec deploy/gitdoc-<slug>-rag -- python -c \
  "import os, psycopg; psycopg.connect(os.environ['POSTGRES_DSN'], connect_timeout=5); print('ok')"
```

- `role does not exist` / `password authentication failed` → DSN is
  wrong in the Secret.
- `could not translate host` → DNS / network: is the Postgres service
  name right for this cluster?
- `SSL connection required` → toggle `sslmode=require` vs `prefer` in
  the DSN based on your homelab Postgres TLS setup.

`/readyz` on the rag pod surfaces this as an HTTP 503 with the
underlying message — tail the logs or hit `/readyz` directly.

### Metrics not showing in Prometheus

- `/metrics` on the rag Service is unconditional — `curl` it from a
  debug pod to confirm:
  `kubectl -n <ns> exec <any-pod> -- wget -qO- http://gitdoc-<slug>-rag:8000/metrics | head`
- If metrics are there but Prom isn't scraping: enable
  `observability.serviceMonitor.enabled=true` AND add the matching
  label (e.g. `release: kube-prometheus-stack`) to
  `observability.serviceMonitor.labels`.

---

## Branch protection / CI

Required status checks on `main`:

- `Lint + unit tests`
- `Build images (verification, no push) (discord-bot)`
- `Build images (verification, no push) (rag)`
- `Build images (verification, no push) (ingestion)`
- `Integration tests`

Release workflow (`.github/workflows/release.yml`) fires on every `v*`
tag push and uses `secrets.GITHUB_TOKEN` — no manual setup. First-time
publish per package: set each `gitdoc-*` package to **Public** in
GitHub Packages → Settings so clusters can pull without a `regcred`
secret.

---

## Post-incident learnings

First-deploy shakedown happened over v0.0.1 → v0.0.10. Quick summary of
what each release fixed, for context when reading logs or git history:

| Tag | Root cause |
|---|---|
| `v0.0.2` | `-migrations` ConfigMap wasn't a Helm hook — Job started before it existed |
| `v0.0.3` | rag Dockerfile missed `reranker.py`; provision SQL didn't grant `public` schema ownership under PG 15+ |
| `v0.0.4` | `GIT_TOKEN` embedded in clone URL leaked into stdout → Loki |
| `v0.0.5` | chart drift (`instance_settings` missing); bot echoed raw tracebacks to Discord; env-var chat model default removed in favour of `/model set` |
| `v0.0.6` | ingestion re-embedded everything every cron tick even when SHA unchanged |
| `v0.0.7` | `Message.create_thread` on a WebhookMessage without guild |
| `v0.0.8` | answers truncated at 1990 chars instead of split across messages |
| `v0.0.9` | `interaction.channel` is a PartialMessageable; had to resolve a real TextChannel via `guild.get_channel` |
| `v0.0.10` | permission 403 on `create_thread` was ERROR-level; should be WARNING with a graceful inline fallback |

Patterns worth remembering:

1. **Docker `COPY` drift is silent.** Unit tests pass (source tree is
   complete); the image breaks on import. CI now runs
   `python -c "import <module>"` inside every built image as a smoke
   step (v0.0.3+).
2. **Chart values aren't validated against deploy-time reality.**
   Hardcoded defaults like `CHAT_MODEL` masked the real problem (that
   LiteLLM might not expose the alias). Moved to runtime validation via
   `/model set`.
3. **Secrets in argv/URL are always a mistake.** `GIT_TOKEN` in the
   clone URL worked fine until it didn't. HTTP Authorization header
   via `GIT_CONFIG_*` env vars never touches process listings or git's
   own error output.
4. **Discord webhook messages are weird.** Any API call that depends on
   `Message.guild` needs a real `TextChannel` resolved via
   `guild.get_channel`, not `interaction.channel`.
5. **Graceful-degrade WARNING >> ERROR for user-fixable config.**
   403 on thread creation isn't a bug; it's a perm the operator hasn't
   granted yet. Don't flood Grafana with ERROR on it.

---

## NetworkPolicy (deferred)

Not yet shipped. When it lands:

- Egress from the rag pod to Postgres + LiteLLM only
- Egress from ingestion to Postgres + LiteLLM + `github.com:443`
  (or GitLab, wherever repos live) only
- Ingress to rag on port 8000 from the bot pod only (when webhook is
  off); additionally from the ingress controller when `webhook.enabled`
- Nothing from/to the bot pod except outbound to
  `gateway.discord.gg:443`

"`/ask` suddenly stopped working after an unrelated change" → suspect
NetworkPolicy on rag egress once this lands.

---

## Next steps for this runbook

- Validated on `mylab` instance through v0.0.10 — every procedure above
  has been executed at least once.
- Gaps that need a real incident to fill: multi-instance version skew
  rollback, Postgres backup/restore (reuses cluster-level backups —
  document when they're exercised).
- If you find a procedure here is wrong or missing, update this file
  in the same commit as whatever fix you ship.
