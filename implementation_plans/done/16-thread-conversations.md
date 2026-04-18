---
status: todo
phase: 4
priority: low
---

# 16 · Thread-aware conversations

## Goal
When a user replies in a Discord thread started by `/ask`, treat the thread as an ongoing conversation: include prior turns in the prompt so follow-ups ("show me the other option", "expand that example") work naturally.

## Why
Single-turn `/ask` forces users to re-phrase their full question every time. For anything more than a one-shot lookup, threading produces much better UX.

## Acceptance criteria
- [ ] Bot detects messages in threads it started and handles them without the `/ask` command.
- [ ] Prior turns (last N messages in the thread) are included in the prompt, with a token budget so long threads don't balloon.
- [ ] Citations from prior turns are preserved in the context but compacted (path + line range, not full content).
- [ ] Thread auto-archives after M minutes of inactivity.
- [ ] Opt-out: `/ask` with a flag to force single-turn behavior.

## Implementation notes
- Discord thread creation is automatic if the bot uses `interaction.followup.send(..., thread_name=...)`; otherwise create it explicitly.
- Prior-turn context is just more rows in the `messages` array to the LLM; no architectural change to retrieval.
- Watch the token count — a long thread plus retrieval context can blow past the model's window quickly.

## Dependencies
- 04 (baseline working)
