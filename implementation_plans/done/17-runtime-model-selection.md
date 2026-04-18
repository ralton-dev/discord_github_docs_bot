---
status: todo
phase: 4
priority: medium
---

# 17 · Runtime model selection via Discord

## Goal
Allow bot users to list and switch the **chat** model at runtime via Discord slash commands, persisted per-instance in Postgres. The `models.chat` value in `values.yaml` becomes the bootstrap default; once a user picks a model via `/model set`, that choice wins until changed again.

## Why
Iterating on which LLM to use shouldn't require a Helm upgrade. Switching is also useful for cost/quality experiments per repo (e.g. cheap local Ollama for a quiet repo, cloud Claude for a busy one).

## Acceptance criteria
- [ ] DB migration adds an `instance_settings` table (key/value, single row per `repo`) — at minimum a `chat_model` column. Apply via the existing Helm `db-migrate` Job.
- [ ] `GET /models` on rag service proxies LiteLLM's `/v1/models` endpoint and returns the list (cached for ~60s to avoid hammering LiteLLM).
- [ ] `GET /settings?repo=<slug>` and `POST /settings` on rag service read/write the active chat model for an instance.
- [ ] `/ask` (existing) reads the active chat model from `instance_settings` first, falls back to `CHAT_MODEL` env var if unset.
- [ ] New bot slash commands:
  - `/model list` — paginated list of available models from the orchestrator.
  - `/model current` — show the currently-selected model.
  - `/model set <name>` — set the active model. Tab-completion drawn from `/v1/models`.
- [ ] Permission gate: `/model set` is restricted to members with Discord's built-in **Manage Server** (`manage_guild`) permission. Read-only commands (`list`, `current`) are open to anyone in the guild. No custom roles or user-ID allowlists.
- [ ] **Embedding model is NOT runtime-changeable.** Document explicitly in the README and in the `/model` help text — switching it would invalidate every stored vector and require full re-ingestion.
- [ ] Unit tests cover the settings endpoints and the slash-command handlers (mocking httpx + the DB).

## Implementation notes
- Schema sketch:
  ```sql
  CREATE TABLE IF NOT EXISTS instance_settings (
      repo        TEXT PRIMARY KEY,
      chat_model  TEXT,
      updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_by  TEXT
  );
  ```
- Permission check uses `discord.Permissions.manage_guild` on the invoking member at command time. If a future user wants finer control (a custom role or a user allowlist), it can be added then — keeping it simple for now.
- LiteLLM's `/v1/models` returns a permissive list; consider letting the chart trim it via a `models.allowList` regex if a deployment wants to hide options users shouldn't pick.
- Cache the `/v1/models` response in-process (TTL=60s) — the list changes rarely and the bot will hit it on every tab-completion.

## Open questions
- **Per-guild vs per-instance.** Each Helm release already runs one repo / one bot, so per-instance is the simpler default. Confirmed.
- **Audit trail.** `updated_by` records the Discord user ID; surface this in `/model current` output? (Probably yes — accountability.)

## Dependencies
- Needs **04 (deploy first instance)** to validate against a live LiteLLM.
- Touches `services/rag/app.py`, so it should slot **after** the Wave 5 chain (11 → 12 → 13 → 14) finishes, OR be done as a small standalone PR before that chain starts. Recommended: do it as part of Wave 4 (parallel with 09/10/15/16); the Wave 5 chain can rebase if needed.
