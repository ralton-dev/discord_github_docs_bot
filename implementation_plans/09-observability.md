---
status: todo
phase: 2
priority: medium
---

# 09 · Observability

## Goal
Expose Prometheus metrics from the RAG orchestrator and emit structured JSON logs from all services, so per-query cost and retrieval quality can be monitored over time.

## Why
Without metrics, runaway token spend is invisible until the LLM provider emails. Without structured logs, debugging a bad answer means scrolling raw stdout.

## Acceptance criteria
- [ ] RAG orchestrator exposes `/metrics` with at least: `gitdoc_queries_total{repo,status}`, `gitdoc_tokens_prompt_total{repo,model}`, `gitdoc_tokens_completion_total{repo,model}`, `gitdoc_retrieval_hits{repo}`, `gitdoc_latency_seconds` histogram.
- [ ] Discord bot logs query ID, guild ID, repo, response length, outcome.
- [ ] Ingestion logs start, per-batch progress, total chunks, GC count, duration.
- [ ] All logs are JSON with a consistent `{"level", "msg", "ts", ...}` shape.
- [ ] Helm chart ships a `ServiceMonitor` (if Prometheus Operator is installed) gated behind a values flag.

## Implementation notes
- `prometheus-fastapi-instrumentator` covers the bulk of orchestrator metrics out of the box.
- Structured logs: drop `logging.basicConfig` in favor of `python-json-logger` or stdlib `logging` with a JSON formatter.
- The 4 most valuable alerts, once metrics land: token burn > threshold, p99 latency > 10s, retrieval hits == 0 ratio > 10%, ingestion failure rate > 0.

## Dependencies
- 04 (need a live instance to verify metrics actually scrape)
