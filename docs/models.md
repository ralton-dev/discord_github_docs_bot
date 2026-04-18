# Runtime model selection

The chat model an instance uses is settable **at runtime** via Discord slash
commands — no redeploy, no values-file edit. The chart's `models.chat`
value is only the *bootstrap default*; once `/model set` has been used, the
per-instance choice lives in Postgres (`instance_settings.chat_model`) and
wins on every subsequent `/ask`.

Embedding models are **not** runtime-selectable — changing the embedding
model invalidates every stored vector and requires a full re-ingest. That's
still a chart value (`models.embed`), fixed at install time.

## Commands

### `/model list`

Lists the model ids exposed by the LiteLLM backend. Cached for 60s on the
orchestrator + 30s on the bot, so autocomplete doesn't hammer LiteLLM.
Ephemeral (only the caller sees the reply).

### `/model current`

Shows the active chat model for this instance, plus when it was last
changed and by whom. Output format:

```
Active model: `ollama_chat/llama3.2:3b`
Last changed: 2026-04-18T13:14:07+00:00 by @user
```

If nothing has been set, shows "Using the chart default (no per-instance
override set)." Ephemeral.

### `/model set <name>`

Persists `name` as the active chat model for this instance. The bot
validates via autocomplete against the live `/models` list; the
orchestrator revalidates server-side and returns 400 with the available
list if the name is unknown. Ephemeral.

**Permission gate:** the caller must have Discord's built-in **Manage
Server** permission (`manage_guild`). No custom roles or user allowlists.
If you want to delegate model management without giving someone full
server-admin, create a Discord role with Manage Server and assign it.

## Rotation

Model switches take effect on the NEXT `/ask`. The orchestrator caches the
per-repo model lookup for 15 seconds, so there's a short window where a
just-in-flight query finishes on the old model.

## Known limits

- Per-instance, not per-channel or per-user.
- No history (only the current active model is recorded; no "list of
  previous models").
- If LiteLLM's `/models` list changes between autocomplete and `set`, the
  validation is rerun server-side — you'll get a 400 if the model
  disappeared mid-flight.
- Cache TTLs (60s models, 15s settings) are in-process and not shared
  across `ragOrchestrator.replicas`; two replicas may briefly disagree on
  the active model for up to 15 seconds after a `/model set`.

## Troubleshooting

- **"unknown model: X"** — LiteLLM doesn't expose that alias. Check the
  LiteLLM proxy config; `/model list` shows what *is* exposed.
- **`/model set` succeeds but `/ask` still uses the old model** — wait up
  to 15s for the per-repo cache to expire, or restart the rag Deployment
  to force-refresh.
- **"This command requires Manage Server permission"** — the caller
  doesn't have the Discord permission. Ask a server admin to either grant
  it or run the command themselves.
