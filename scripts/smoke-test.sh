#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# gitdoc end-to-end smoke test.
#
# Verifies a freshly-deployed instance is healthy by:
#   1. waiting for every pod in $NAMESPACE to be Ready
#   2. triggering an ad-hoc ingestion Job from the chart's CronJob
#   3. waiting for that Job to complete (dumping logs on failure)
#   4. port-forwarding the rag service and POSTing /ask
#   5. asserting the response has a non-empty `answer` and at least one citation
#   6. exec'ing into the rag pod and counting chunks for the repo in Postgres
#
# Required env (or override on the command line):
#   NAMESPACE   k8s namespace, e.g. gitdoc-project-a
#   RELEASE     helm release name, e.g. gitdoc-project-a
# Optional:
#   QUERY       smoke-test question (default: "what is this project about?")
#   PORT        local port for the port-forward (default: 18000)
#   ASK_TIMEOUT seconds to wait for /ask to return (default: 90)
#   POD_TIMEOUT seconds to wait for pods Ready (default: 300)
#   JOB_TIMEOUT seconds to wait for ingest Job (default: 1800)
#
# Exit codes:
#   0 — all checks passed
#   1 — pre-flight failed (missing tooling, bad args)
#   2 — pods didn't become Ready in time
#   3 — ingestion Job failed or timed out
#   4 — /ask request failed or response shape was wrong
#   5 — DB chunk count was zero after ingestion
# ---------------------------------------------------------------------------
set -euo pipefail

# --- args / defaults -------------------------------------------------------
: "${NAMESPACE:?NAMESPACE is required, e.g. NAMESPACE=gitdoc-project-a}"
: "${RELEASE:?RELEASE is required, e.g. RELEASE=gitdoc-project-a}"
QUERY="${QUERY:-what is this project about?}"
PORT="${PORT:-18000}"
ASK_TIMEOUT="${ASK_TIMEOUT:-90}"
POD_TIMEOUT="${POD_TIMEOUT:-300}"
JOB_TIMEOUT="${JOB_TIMEOUT:-1800}"

INGEST_JOB="${RELEASE}-ingest-smoke-$(date +%s)"
RAG_DEPLOY="${RELEASE}-rag"

log()  { printf '\033[1;34m[smoke]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; }

# --- pre-flight ------------------------------------------------------------
for cmd in kubectl curl jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    fail "missing required tool: $cmd"
    exit 1
  fi
done

if ! kubectl -n "$NAMESPACE" get ns >/dev/null 2>&1; then
  # `get ns` against a namespaced subject returns the namespace object.
  if ! kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
    fail "namespace $NAMESPACE not found"
    exit 1
  fi
fi

PORT_FORWARD_PID=""
cleanup() {
  if [[ -n "$PORT_FORWARD_PID" ]] && kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
    kill "$PORT_FORWARD_PID" 2>/dev/null || true
    wait "$PORT_FORWARD_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- step 1: pods ready ----------------------------------------------------
log "step 1/6 — listing pods in $NAMESPACE"
kubectl -n "$NAMESPACE" get pods -o wide || true

log "step 1/6 — waiting up to ${POD_TIMEOUT}s for all pods to be Ready"
# wait for the workload pods (Deployments only — the db-migrate Job pod is
# already gone after pre-install, and CronJob pods only exist mid-run).
if ! kubectl -n "$NAMESPACE" wait \
      --for=condition=Ready pod \
      -l "app.kubernetes.io/name=gitdoc,app.kubernetes.io/instance=${RELEASE}" \
      --timeout="${POD_TIMEOUT}s"; then
  fail "pods did not become Ready in ${POD_TIMEOUT}s"
  kubectl -n "$NAMESPACE" get pods
  kubectl -n "$NAMESPACE" describe pods \
    -l "app.kubernetes.io/name=gitdoc,app.kubernetes.io/instance=${RELEASE}" \
    | tail -120
  exit 2
fi
ok "all pods Ready"

# --- step 2: trigger ingestion --------------------------------------------
log "step 2/6 — triggering ad-hoc ingestion: $INGEST_JOB"
kubectl -n "$NAMESPACE" create job "$INGEST_JOB" \
  --from="cronjob/${RELEASE}-ingest"

# --- step 3: wait for ingestion to complete --------------------------------
log "step 3/6 — waiting up to ${JOB_TIMEOUT}s for $INGEST_JOB to complete"
# `wait --for=condition=Complete` returns 0 on Complete, non-zero otherwise.
# We catch the failure and dump logs from the Job's pod for diagnosis.
if ! kubectl -n "$NAMESPACE" wait \
      --for=condition=Complete \
      "job/${INGEST_JOB}" \
      --timeout="${JOB_TIMEOUT}s"; then
  fail "ingestion Job did not complete in ${JOB_TIMEOUT}s"
  kubectl -n "$NAMESPACE" describe "job/${INGEST_JOB}" || true
  kubectl -n "$NAMESPACE" logs --tail=200 -l "job-name=${INGEST_JOB}" || true
  exit 3
fi
ok "ingestion Job completed"
kubectl -n "$NAMESPACE" logs --tail=20 -l "job-name=${INGEST_JOB}" || true

# --- step 4: port-forward + POST /ask -------------------------------------
log "step 4/6 — port-forwarding ${RAG_DEPLOY} to localhost:${PORT}"
kubectl -n "$NAMESPACE" port-forward "deploy/${RAG_DEPLOY}" "${PORT}:8000" \
  >/tmp/smoke-pf.log 2>&1 &
PORT_FORWARD_PID=$!

# Wait for the forward to actually be listening.
for _ in $(seq 1 30); do
  if curl -sf "http://localhost:${PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! curl -sf "http://localhost:${PORT}/healthz" >/dev/null 2>&1; then
  fail "port-forward never became reachable; pf log:"
  cat /tmp/smoke-pf.log >&2 || true
  exit 4
fi
ok "port-forward up; /healthz reachable"

# Derive REPO from the chart's ConfigMap so the smoke test doesn't need it
# passed in.
REPO="$(kubectl -n "$NAMESPACE" get configmap "${RELEASE}-config" \
        -o jsonpath='{.data.REPO_NAME}')"
if [[ -z "$REPO" ]]; then
  fail "could not derive REPO_NAME from configmap ${RELEASE}-config"
  exit 4
fi
log "step 4/6 — POST /ask repo=${REPO} query='${QUERY}'"

ASK_BODY="$(jq -nc --arg q "$QUERY" --arg r "$REPO" \
  '{query:$q, repo:$r, top_k:6}')"
RESP="$(curl -sS --max-time "${ASK_TIMEOUT}" \
  -H 'content-type: application/json' \
  -X POST "http://localhost:${PORT}/ask" \
  -d "$ASK_BODY")" || {
    fail "/ask request failed; response so far: $RESP"
    exit 4
  }

ANSWER="$(printf '%s' "$RESP" | jq -r '.answer // empty')"
CITES="$(printf '%s'  "$RESP" | jq -r '.citations | length // 0')"

if [[ -z "$ANSWER" ]]; then
  fail "/ask returned empty answer; full response below"
  printf '%s\n' "$RESP" >&2
  exit 4
fi
if [[ "$CITES" -lt 1 ]]; then
  fail "/ask returned 0 citations (groundedness check failed); full response below"
  printf '%s\n' "$RESP" >&2
  exit 4
fi
ok "/ask returned $(printf '%s' "$ANSWER" | wc -c) chars + ${CITES} citation(s)"

# --- step 5: DB sanity check ---------------------------------------------
log "step 5/6 — counting chunks for repo='${REPO}' via the rag pod"
RAG_POD="$(kubectl -n "$NAMESPACE" get pod \
  -l "app.kubernetes.io/instance=${RELEASE},app.kubernetes.io/component=rag" \
  -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$RAG_POD" ]]; then
  fail "no rag pod found"
  exit 5
fi
# psql isn't in the rag image, so we ask Python to do the count using the
# DSN already in env. Keeps us from shelling out to a sidecar.
COUNT="$(kubectl -n "$NAMESPACE" exec "$RAG_POD" -- python -c "
import os, psycopg
with psycopg.connect(os.environ['POSTGRES_DSN'], connect_timeout=5) as c:
    print(c.execute('SELECT count(*) FROM chunks WHERE repo=%s', (os.environ['REPO_NAME'],)).fetchone()[0])
" 2>/tmp/smoke-db.err)" || {
    fail "DB chunk count failed; stderr:"
    cat /tmp/smoke-db.err >&2 || true
    exit 5
  }
COUNT="${COUNT//[[:space:]]/}"
if ! [[ "$COUNT" =~ ^[0-9]+$ ]] || (( COUNT < 1 )); then
  fail "expected at least 1 chunk for repo=${REPO}, got '${COUNT}'"
  exit 5
fi
ok "chunks(${REPO}) = ${COUNT}"

# --- step 6: latest ingest_runs row ---------------------------------------
log "step 6/6 — verifying latest ingest_runs row is status=ok"
RUN_STATUS="$(kubectl -n "$NAMESPACE" exec "$RAG_POD" -- python -c "
import os, psycopg
with psycopg.connect(os.environ['POSTGRES_DSN'], connect_timeout=5) as c:
    row = c.execute(\"SELECT status, chunk_count FROM ingest_runs WHERE repo=%s ORDER BY id DESC LIMIT 1\", (os.environ['REPO_NAME'],)).fetchone()
    print((row or ('missing', 0))[0], (row or ('missing', 0))[1])
" 2>/tmp/smoke-db.err)" || {
    fail "ingest_runs lookup failed; stderr:"
    cat /tmp/smoke-db.err >&2 || true
    exit 5
  }
if [[ "${RUN_STATUS%% *}" != "ok" ]]; then
  fail "latest ingest_runs status is '${RUN_STATUS}', expected 'ok ...'"
  exit 5
fi
ok "ingest_runs latest = ${RUN_STATUS}"

echo
ok "SMOKE TEST PASSED — ${RELEASE} in ${NAMESPACE} is healthy"
