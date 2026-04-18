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

---

## Appendix: Drafted from task 16

> Captured while wiring up thread-aware conversations in
> `services/discord-bot/bot.py`. When task 05 writes the full runbook,
> fold this into the "Daily ops / Discord" section.

- **Discord developer-portal step (mandatory before deploy).** The bot
  now reads message bodies in threads it started, which is a
  **privileged** intent. In `bot.py` we set
  `intents.message_content = True`, but Discord *also* requires the
  operator to enable it in the dev portal:

  > Discord Developer Portal -> Applications -> *<app>* -> Bot ->
  > "Privileged Gateway Intents" section -> toggle
  > **MESSAGE CONTENT INTENT** on -> Save Changes.

  If this is left off, the bot logs in fine and `/ask` works, but
  follow-ups in threads silently see empty `message.content` and
  appear to be ignored. Make this the first item in any "bot replies
  to /ask but ignores follow-ups" troubleshooting tree.

- **Killing a runaway thread.** If a thread starts looping or accrues
  unwanted noise, delete it from Discord directly (right-click thread
  -> *Delete Thread*, or via REST: `DELETE /channels/{thread_id}`).
  Thread deletion is final — the bot does not need to be involved and
  has no in-bot kill switch. To temporarily mute the bot in one
  thread, archive it (right-click -> *Archive Thread*); the bot will
  not send into archived threads and the next `/ask` opens a fresh
  one. Auto-archive is set to 60 minutes of inactivity, so most stale
  threads clean themselves up.

- **`/ask single:true` overrides everything.** If a user (or operator
  testing) wants the original single-turn behaviour without any thread
  creation or follow-up handling, they pass `single:true` to the slash
  command. Use this:
    1. As the workaround when the orchestrator is misbehaving on
       multi-turn (until plan 11+ stabilises).
    2. To probe whether a bug is in single-turn `/ask` or in the
       follow-up path — if `single:true` works and the default doesn't,
       the bug is in `on_message` / `_collect_thread_history` /
       `_ask_orchestrator` history forwarding.

- **Auto-archive duration.** Set via `auto_archive_duration=60` (60
  minutes) in `Message.create_thread`. Discord only accepts
  `60 / 1440 / 4320 / 10080` (1h / 1d / 3d / 7d). To change globally,
  edit `THREAD_AUTO_ARCHIVE_MINUTES` in `bot.py` (no chart change
  needed) and roll the bot deployment.

- **How the bot identifies "its own" threads.** `on_message` checks
  three things in order:
    1. `isinstance(message.channel, discord.Thread)` — message is in a
       thread (not a regular channel or DM).
    2. `_is_bot_thread(thread)` — the thread's starter message author
       equals `client.user`. We try the cached `thread.starter_message`
       first; if the cache is cold we fetch the message from the parent
       channel (a thread's ID equals its starter message's ID, which
       makes the lookup free of any bookkeeping).
    3. `message.author.bot` is False — never react to other bots, our
       own retries, or webhook chatter.

  If you see the bot replying in threads it didn't open, suspect (2):
  someone reused a thread name in a channel where the bot once
  answered. The starter-message check should still keep us out, but
  log inspection (`grep "rag call failed for thread follow-up"`) is
  the fastest confirmation.

- **History token-budget tuning.** Currently `HISTORY_TURN_LIMIT = 10`
  and `HISTORY_CHAR_BUDGET = 3000` (chars, not tokens — a deliberate
  conservative proxy at roughly 1 char ≈ 0.25 tokens). Bump in
  `bot.py` if a particular instance needs longer multi-turn memory
  and the chosen chat model has the context window. **Citations from
  prior bot answers are compacted** to `[src: a.py, b.md, ...]` before
  forwarding — only the file paths survive into history, never the
  full Markdown bullet list with short SHAs.

- **Graceful-degrade on orchestrator 422.** `_ask_orchestrator` retries
  without `history` when the orchestrator rejects the payload (422),
  and logs `"falling back to single-turn"` exactly **once per process**
  (controlled by `_history_unsupported_logged`). The bot never errors
  out on the user when the RAG side hasn't yet shipped multi-turn
  support. Operator action: when the orchestrator is upgraded to
  accept `history`, restart the bot deployment so the latch resets and
  any future genuine 422s log again.

---

## Appendix: Drafted from task 09

> Captured while wiring up metrics + structured logs in `services/rag/`,
> `services/ingestion/`, and `services/discord-bot/`, plus the chart's
> gated `ServiceMonitor`. When task 05 writes the full runbook, fold this
> into the "Day-2 ops / monitoring" section. Full metric/log reference
> lives in `docs/observability.md`; the runbook only needs the
> cluster-operator one-liners below.

- **Reading metrics without Prometheus.** The rag pod exposes `/metrics`
  unconditionally. Fastest sanity check from the cluster:
  ```sh
  kubectl -n gitdoc-<slug> port-forward svc/gitdoc-<slug>-rag 8000:8000
  curl -s http://localhost:8000/metrics | grep -E '^gitdoc_' | head
  ```
  Use this when "is the service actually recording metrics?" needs an
  answer before deciding to blame the scraper.

- **ServiceMonitor troubleshooting.** When `observability.serviceMonitor.enabled=true`
  renders the object but Prometheus is not scraping, the default cause is
  `serviceMonitorSelector` on the Prometheus CR. Check:
  ```sh
  kubectl -n <prom-ns> get prometheus -o jsonpath='{.items[*].spec.serviceMonitorSelector}'
  ```
  then add matching labels via `observability.serviceMonitor.labels` in
  the values file and `helm upgrade`.

- **Log querying cheat-sheet.** JSON-shaped stdout makes `jq` the right
  tool. Canonical queries every operator will run:
  ```sh
  # "Why did /ask return an error?" — pull the last few ERROR lines.
  kubectl -n gitdoc-<slug> logs deploy/gitdoc-<slug>-rag --tail=500 \
    | jq -c 'select(.level == "ERROR")'

  # "What was slow?" — the 10 slowest completions in the last 500 lines.
  kubectl logs deploy/gitdoc-<slug>-rag --tail=500 \
    | jq -c 'select(.event == "ask.completed")' \
    | jq -s 'sort_by(-.latency_ms) | .[:10]'

  # "Trace a single user question." — grab the query_id from the bot's
  # bot.ask event, then filter bot + rag logs by it.
  kubectl logs deploy/gitdoc-<slug>-bot --tail=200 \
    | jq -c 'select(.event == "bot.ask")'
  # -> note a query_id, then:
  Q=<query_id>
  kubectl logs deploy/gitdoc-<slug>-bot --tail=500 \
    | jq -c "select(.query_id == \"$Q\")"
  ```

- **Ingestion run summaries live in logs AND in the `ingest_runs` table.**
  For cron-on-schedule, `ingest.complete` emits `duration_ms`, `chunks`,
  `deleted` — the fast triage path. For historical comparison (e.g.
  "has throughput regressed?") query:
  ```sql
  SELECT repo,
         count(*)        AS runs,
         avg(chunk_count) AS avg_chunks,
         max(finished_at - started_at) AS slowest
  FROM ingest_runs
  WHERE status = 'ok' AND started_at > now() - interval '7 days'
  GROUP BY repo;
  ```

- **Token-cost sanity check.** Multiplying the counters by the LiteLLM
  cost table is the "are we on budget?" first-order answer:
  ```promql
  sum(increase(gitdoc_tokens_completion_total[1d])) by (model)
  ```
  divided by whatever the provider charges per million tokens. Build a
  per-repo dashboard row here — the labels are already in place.

- **Metric naming contract.** `gitdoc_*` prefix on every exposed metric.
  Do NOT add a metric without a `repo` label on business counters — the
  homelab runs one rag per repo and we need per-instance rollups. If a
  new metric is truly repo-independent (e.g. a worker-pool gauge), omit
  the label rather than hardcoding it. The handful of metrics defined
  today are in `services/rag/metrics.py`.

- **JSON log field stability.** Downstream dashboards / alerts will
  grep on the `event` key. Changing the **name** of an event is a
  breaking change; adding fields is free. Rename events only with a
  deprecation window (emit both the old and new name for one release
  cycle).

- **What metrics to wire into alerts first.** Four alerts identified in
  the task brief: token-burn rate, p99 ask latency, zero-retrieval ratio,
  ingestion failure. PromQL templates for all four live in
  `docs/observability.md` — copy them into the operator's alerting stack
  verbatim, tune thresholds to the instance.

---

## Appendix: Drafted from task 17

> Captured while wiring runtime chat-model selection through the Discord
> bot, the rag orchestrator, and the `instance_settings` Postgres table.
> When task 05 writes the full runbook, fold this into the "Daily ops"
> and "Debugging" sections.

- **Model switch procedure.** `/model set <name>` from Discord (requires
  Manage Server permission). Takes effect on the next `/ask` — within
  ~15s because of the per-repo orchestrator cache. No redeploy, no
  values-file edit.
- **Chart `models.chat` is only the bootstrap default.** Once
  `instance_settings.chat_model` is populated (via `/model set`), the
  DB wins. Bumping `models.chat` and `helm upgrade`-ing the chart has
  **no effect** on instances that already set the override. To force a
  chart-wide default change, also `UPDATE instance_settings SET
  chat_model = NULL WHERE repo = '<slug>'` per instance.
- **Embedding model is still static.** `models.embed` + `models.embedDim`
  are fixed at install time. Runtime changes would invalidate every
  vector in the `chunks` table. Never exposed via `/model`.
- **Troubleshoot "unknown model".** If `/model set` 400s:
  `kubectl -n gitdoc-<slug> exec deploy/gitdoc-<slug>-rag -- curl -s
  http://localhost:8000/models | jq '.data[].id'` shows what LiteLLM is
  actually exposing under the current aliases. Fix the LiteLLM config
  if the intended alias is missing.
- **Stale model after switch.** Two replicas of `rag` have independent
  15s caches, so for ~15s after `/model set` different queries can
  resolve to different models. Not worth a shared cache at homelab scale
  but flag if users report inconsistency.
- **Audit trail.** `instance_settings.updated_by` records the Discord
  user snowflake. `SELECT repo, chat_model, updated_at, updated_by FROM
  instance_settings` is the audit query; feed it into the runbook's
  "who changed what" recipe.
- **Decommissioning an instance.** `revoke-instance.sql` drops the DB
  which includes `instance_settings`. No separate cleanup required.

---

## Appendix: Drafted from task 11

> Captured while wiring hybrid search (BM25 + vector via Reciprocal Rank
> Fusion) into `services/rag/app.py` and the chart. When task 05 writes
> the full runbook, fold this into the "Daily ops / retrieval" and
> "Debugging" sections. Full retrieval reference lives in
> `docs/retrieval.md`; the runbook only needs the operator one-liners
> below.

- **The `chunks.content_tsv` column is generated.** Postgres keeps it
  in sync on every INSERT/UPDATE — there is no trigger to manage and
  ingestion code is unchanged. The migration is idempotent
  (`ADD COLUMN IF NOT EXISTS`); `helm upgrade --install` picks it up
  via the existing pre-install/upgrade migration Job.
- **Backfill on existing instances is automatic.** When the migration
  Job runs `ALTER TABLE ... ADD COLUMN ... GENERATED ... STORED`,
  Postgres backfills every existing row in one pass. On a homelab-
  scale corpus (≤ 1M chunks) this is seconds; on a much larger one,
  schedule the upgrade off-peak — the table is exclusively locked for
  the duration of the backfill.
- **Disable hybrid per-instance** when isolating a fusion-related
  regression:
  ```yaml
  # values-<slug>.yaml
  search:
    hybrid:
      enabled: false
  ```
  then `helm upgrade --install`. The vector-only path
  (`_retrieve_vector`) stays in the codebase and is exercised by the
  same `/ask` request shape, so the rollback is a config change, not a
  code change.
- **Verifying the index exists.** Quick check after a deploy:
  ```sh
  kubectl -n gitdoc-<slug> exec deploy/gitdoc-<slug>-rag -- python -c \
    "import os, psycopg; \
     c = psycopg.connect(os.environ['POSTGRES_DSN']); \
     print(c.execute(\"SELECT indexname FROM pg_indexes WHERE tablename='chunks'\").fetchall())"
  ```
  Expect `chunks_content_tsv_idx` in the list. If it's missing, the
  pre-upgrade migration Job didn't run — check `kubectl get jobs
  -n gitdoc-<slug>` for `gitdoc-<slug>-db-migrate` failures.
- **"Why didn't BM25 find my exact-string query?"** Postgres's
  `to_tsvector('english', ...)` applies English stemming and drops
  stop-words. `process_batch` is split into `process` + `batch` (and
  stemmed). Stop-words like `the`, `is`, `a` are dropped from the
  query too. For corpora where this matters, switch the english
  config to `simple` in `_retrieve_hybrid` — one-line change, requires
  re-running the migration to drop and re-create `content_tsv` so the
  generated expression matches.
- **RRF tuning is rarely needed.** `RRF_K = 60` is the canonical value.
  Lower `k` (20-30) makes top-of-list ranks dominate; higher `k`
  (100+) flattens contribution. Don't tune without an evaluation set
  — the constant is the kind of knob that's easy to fiddle and hard
  to verify.
- **Seam reserved for tasks 12-14.** `_retrieve` returns ordered rows
  and is not allowed to mutate the chat call path — task 12
  (reranker) wraps its output. The TOP of the `/ask` handler, before
  the embedding call, is reserved as the insertion point for task 13
  (query cache short-circuit) and task 14 (rate-limit gate).

---

## Appendix: Drafted from task 12

> Captured while wiring the cross-encoder reranker (`reranker.py` +
> `RERANK_LATENCY_SECONDS` + chart values) between retrieval and the LLM
> call in `services/rag/app.py`. When task 05 writes the full runbook,
> fold this into the "Daily ops / retrieval" and "Debugging" sections.
> Full reranker reference lives in `docs/retrieval.md`; the runbook only
> needs the operator one-liners below.

- **The reranker is OFF by default.** No effect on existing instances
  until `reranker.enabled=true` AND `reranker.url=<reachable endpoint>`
  are both set in the per-instance values file. The empty-URL
  short-circuit means a half-configured rollout silently falls back to
  un-reranked retrieval — no broken /ask requests, but also no quality
  win. Confirm both via `kubectl get cm gitdoc-<slug>-config -o yaml |
  grep RERANKER_`.

- **Provisioning a reranker endpoint.** Two routes:
  1. **Ollama** — pull `bge-reranker-v2-m3` and point at the host. Some
     Ollama builds expose `/api/rerank` directly; older builds need a
     small adapter in front. The integration test in
     `tests/integration/test_e2e.py` uses a fake reranker so CI does
     not need a live endpoint.
  2. **LiteLLM `/v1/rerank`** — already in the cluster. Response shape
     differs (`{"results": [{"index": int, "relevance_score": float}]}`
     vs our `{"scores": [float]}`), so put a thin FastAPI adapter in
     front and point `reranker.url` at the adapter.

- **Verifying the reranker is being called.** `gitdoc_rerank_latency_seconds`
  has zero observations until at least one /ask runs against an enabled
  pod. Quick check:
  ```sh
  kubectl -n gitdoc-<slug> port-forward svc/gitdoc-<slug>-rag 8000:8000
  curl -s http://localhost:8000/metrics | grep gitdoc_rerank_latency
  ```
  Sum > 0 = the reranker is in the path. If the sum stays at 0 after
  several /ask calls, either `RERANKER_ENABLED` is false or
  `RERANKER_URL` is empty in the rendered ConfigMap.

- **Spotting graceful-degrade in the wild.** When the reranker errors,
  the rag pod logs `WARN`-level events with `event` fields starting
  `rerank.` — `rerank.transport_error`, `rerank.http_error`,
  `rerank.malformed_response`, `rerank.bad_json`, `rerank.non_numeric_scores`.
  Operator one-liner:
  ```sh
  kubectl -n gitdoc-<slug> logs deploy/gitdoc-<slug>-rag --tail=500 \
    | jq -c 'select(.event | startswith("rerank."))'
  ```
  Spike in any of these = the reranker is sick but /ask is still
  working (un-reranked rows). Triage the upstream endpoint first, not
  the rag pod.

- **Latency budget violations.** Buckets in `gitdoc_rerank_latency_seconds`
  are `(0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0)`. Anything in the 2-5s
  bucket needs investigation; the default 5s timeout means a wedged
  endpoint trips graceful-degrade rather than slowing /ask. PromQL for
  "are we beyond budget?":
  ```promql
  histogram_quantile(0.95, sum(rate(gitdoc_rerank_latency_seconds_bucket[5m])) by (le))
  ```
  Target < 0.5s for `top_k=6`. >0.5s sustained = either bump the
  multiplier down, switch to a smaller cross-encoder (`bge-reranker-base`),
  or move the model to a faster host.

- **`candidatesMultiplier` is the recall lever.** Default 3 →
  `top_k * 3` candidates. Bump to 4-5 when the right answer reliably
  lands in the top 18 but not the top 6 (eyeball ingest_runs +
  manual /ask probes; no automated eval set yet). Don't push past 5
  without measuring — the cross-encoder cost scales linearly.

- **Disabling without a redeploy is not a thing.** Env vars are read
  at module import, so flipping the flag requires a `helm upgrade`
  (which rolls the rag pods). Emergency kill-switch: set
  `reranker.url=""` (empty string) in the values file and upgrade —
  `RERANKER_URL` becomes empty, the dispatcher short-circuits, /ask
  returns un-reranked rows immediately. Cleaner than tearing down the
  reranker endpoint itself if it is shared with other services.

- **Seam reserved for tasks 13-14.** The rerank call sits in the
  POST-retrieve / PRE-LLM region of `/ask`. Task 13 (query cache) and
  task 14 (rate limiting) will both insert ABOVE the embedding call —
  separate region, no collision with the reranker block.
