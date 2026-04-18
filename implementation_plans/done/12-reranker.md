---
status: todo
phase: 3
priority: medium
---

# 12 · Reranker

## Goal
Add a reranking step between retrieval and LLM invocation: fetch `top_k * 3` candidates, rerank with a cross-encoder, keep the best `top_k`. Likely model: `BAAI/bge-reranker-v2-m3` served via Ollama.

## Why
Cross-encoder reranking is consistently the highest-impact, lowest-cost RAG quality win. It filters noisy vector hits before they burn expensive LLM context.

## Acceptance criteria
- [ ] Reranker model deployed to the existing Ollama service.
- [ ] RAG orchestrator fetches `top_k * 3` initial hits, calls the reranker via LiteLLM (or directly if Ollama isn't exposed via LiteLLM for rerank), keeps `top_k`.
- [ ] Latency budget: reranking adds < 500ms p95 for `top_k=6`.
- [ ] Integration test asserts a noisy query (e.g., a common word present in many files) lands the right chunk after rerank.
- [ ] Values flag to disable reranking (for A/B testing).

## Implementation notes
- bge-reranker outputs a relevance score per `(query, chunk)` pair; sort descending, keep top `k`.
- Batch the rerank calls: send all `3k` candidates in one request, not one per chunk.
- LiteLLM support for rerank endpoints is spotty; direct Ollama call may be simpler.

## Dependencies
- 04 (baseline working)
