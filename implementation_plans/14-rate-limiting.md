---
status: todo
phase: 3
priority: medium
---

# 14 · Rate limiting & cost caps

## Goal
Enforce per-guild and per-user token budgets so a single runaway user (or a prompt-injection attempt) can't burn the LLM budget.

## Why
Current `GUILD_ALLOWLIST` is a blunt on/off. A guild might be authorized but still shouldn't be able to fire 10k queries overnight. Budget caps are cheap insurance.

## Acceptance criteria
- [ ] `rate_limits` table with rolling-window counters (guild_id, user_id, window_start, tokens_used).
- [ ] Orchestrator checks the window before calling the LLM; over-budget returns a friendly 429-style response.
- [ ] Limits configurable per-instance via values (`rateLimits.guildTokensPerHour`, `rateLimits.userTokensPerHour`).
- [ ] Bot surfaces rate-limit errors distinctly from backend errors ("you're asking too fast, try again in N minutes").
- [ ] Metrics for rate-limit hits exposed.

## Implementation notes
- Sliding window via `(now - interval)` filter is accurate enough; no need for Redis.
- Token counts should use the actual prompt+completion totals returned by LiteLLM, not a pre-estimate.
- Admin role bypass worth considering — a slash command permission flag on `/ask` can gate it.

## Dependencies
- 04 (baseline working)
- 09 (metrics)
