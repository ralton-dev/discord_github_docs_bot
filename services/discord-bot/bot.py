import logging
import os
import re
import uuid

import discord
import httpx
from discord import app_commands

from logging_config import configure as configure_logging

configure_logging()
log = logging.getLogger("gitdoc.bot")

RAG_URL = os.environ["RAG_ORCHESTRATOR_URL"]
REPO    = os.environ["TARGET_REPO"]
TOKEN   = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ALLOWLIST = {
    int(g) for g in os.environ.get("GUILD_ALLOWLIST", "").split(",") if g.strip()
}

# Thread/follow-up tuning constants. Pulled out so tests can patch them
# without monkey-patching strings inside function bodies.
THREAD_NAME_LIMIT = 90              # Discord caps thread names at 100 chars.
THREAD_AUTO_ARCHIVE_MINUTES = 60    # Discord accepts: 60 / 1440 / 4320 / 10080.
HISTORY_TURN_LIMIT = 10             # Last N messages to consider as history.
HISTORY_CHAR_BUDGET = 3000          # Drop oldest until total content <= budget.

# Timeout for /ask calls against the orchestrator. Default 180s because a
# locally-hosted LLM (Ollama) can take 30–90s to cold-load a model on the
# first request, and the bot's 60s was not enough to survive that. Discord
# gives us 15 minutes to respond via followup after defer(thinking=True),
# so 180s is well within the window. Override via the chart's
# `discordBot.askTimeoutSecs`.
ASK_TIMEOUT_SECS = int(os.environ.get("ASK_TIMEOUT_SECS", "180"))

# message_content is a privileged intent. It is required for the bot to read
# the bodies of replies in its threads (those replies do not mention the bot,
# so the message_content intent is mandatory; without it the .content field
# is silently empty). The operator must enable "MESSAGE CONTENT INTENT" in
# the Discord Developer Portal -> Bot tab for the bot's application.
intents = discord.Intents.default()
intents.message_content = True
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

# Module-level latch so we only log the "orchestrator does not support history"
# warning once per process, not once per follow-up.
_history_unsupported_logged = False


def _format(answer: str, citations: list[dict]) -> str:
    cites = "\n".join(
        f"- `{c['path']}` @ `{c['commit_sha'][:7]}`" for c in citations
    ) or "_no sources_"
    msg = f"{answer}\n\n**Sources**\n{cites}"
    # Discord hard-caps messages at 2000 chars.
    return msg if len(msg) <= 1990 else msg[:1987] + "..."


# Matches the bullet line `- \`path\` @ \`sha\`` we emit in `_format`.
_CITATION_LINE_RE = re.compile(r"^-\s+`([^`]+)`\s+@\s+`([^`]+)`\s*$", re.MULTILINE)


def _compact_citations(answer_block: str) -> str:
    """Compact a previously-formatted bot answer's `**Sources**` block.

    Takes the full text we previously sent (i.e. the output of `_format`)
    and returns a one-line summary like `[src: a.py, b.md]`.

    Used to shrink prior-turn citation noise before forwarding history to
    the orchestrator: the LLM only needs to know which files were cited,
    not the full Markdown bullet list with short SHAs.

    Behaviour:
    - No `**Sources**` header -> return empty string.
    - `_no sources_` sentinel  -> `[src: none]`.
    - One or more bullet lines -> `[src: path1, path2, ...]` deduped,
      preserving first-seen order.
    """
    marker = "**Sources**"
    if marker not in answer_block:
        return ""

    sources_block = answer_block.split(marker, 1)[1]
    # Strip the leading newline after the header, if any.
    sources_block = sources_block.lstrip("\n")

    if sources_block.strip().startswith("_no sources_"):
        return "[src: none]"

    paths: list[str] = []
    seen: set[str] = set()
    for match in _CITATION_LINE_RE.finditer(sources_block):
        path = match.group(1)
        if path not in seen:
            seen.add(path)
            paths.append(path)

    if not paths:
        # Header was present but no parseable bullets — be conservative.
        return "[src: none]"
    return "[src: " + ", ".join(paths) + "]"


async def _collect_thread_history(
    thread: "discord.Thread",
    limit: int = HISTORY_TURN_LIMIT,
    char_budget: int = HISTORY_CHAR_BUDGET,
) -> list[dict]:
    """Walk a thread's chronological history and build a chat-style list.

    Returns `[{"role": "user"|"assistant", "content": "..."}, ...]` in
    chronological (oldest-first) order, suitable for sending as `history`
    to the orchestrator's `/ask` endpoint.

    Rules:
    - Excludes the slash-command interaction itself (the thread's starter
      message is the bot's first answer, posted via interaction.followup —
      it is included; the originating user query is NOT in the thread).
    - Includes the most recent `limit` non-bot user messages and the bot's
      own answers, with `assistant` messages compacted via
      `_compact_citations` so prior `**Sources**` blocks don't dominate
      the budget.
    - Drops oldest turns first until the total `len(content)` across all
      returned entries is `<= char_budget`. If a single message alone
      exceeds the budget it is still returned (we'd rather forward
      something than nothing — the orchestrator can truncate again).
    """
    raw: list[dict] = []
    # Pull `limit` newest messages then reverse to chronological order.
    # discord.py's history(limit=N) yields newest-first by default.
    async for msg in thread.history(limit=limit, oldest_first=False):
        if msg.author == client.user:
            content = _compact_citations(msg.content) or msg.content
            role = "assistant"
        else:
            # Skip system/webhook chatter just in case.
            if msg.author.bot:
                continue
            content = msg.content
            role = "user"
        if not content:
            continue
        raw.append({"role": role, "content": content})

    # Re-order to chronological (oldest first).
    raw.reverse()

    # Drop oldest entries while we are over budget. We size by `len(content)`
    # which is a simple, deterministic proxy for tokens — fine for a 3000-char
    # cap that is well below any modern context window.
    def _size(entries: list[dict]) -> int:
        return sum(len(e["content"]) for e in entries)

    while len(raw) > 1 and _size(raw) > char_budget:
        raw.pop(0)

    return raw


class RateLimitedError(Exception):
    """Raised by `_ask_orchestrator` when the orchestrator returns 429.

    Carries the `retry_after` (seconds) and `reason` (`guild_budget` or
    `user_budget`) from the orchestrator's JSON body so the command
    handlers can render a friendly, ephemeral message to the user.
    Distinct from a generic HTTP error so `/ask` and the thread
    follow-up handler can catch it specifically and not lump it in with
    "orchestrator is down" messaging.
    """

    def __init__(self, retry_after: int, reason: str) -> None:
        super().__init__(f"rate limited ({reason}); retry in {retry_after}s")
        self.retry_after = retry_after
        self.reason = reason


def _format_rate_limited_message(err: RateLimitedError) -> str:
    """Canonical user-facing copy for a 429 from the orchestrator."""
    return (
        f"You're asking too fast. Retry in ~{err.retry_after} seconds.\n"
        f"(Budget: {err.reason})"
    )


class ModelNotSetError(Exception):
    """Raised by `_ask_orchestrator` when the orchestrator returns 409
    because no chat model has been configured for this instance.

    Distinct from a generic HTTP error: the remedy is explicit (run
    `/model set`), so the command handlers render a specific nudge
    message instead of "something went wrong".
    """


_MODEL_NOT_SET_MESSAGE = (
    "This bot doesn't have a chat model configured yet.\n"
    "A server admin can run `/model set <name>` to pick one. "
    "Try `/model list` first to see what's available."
)


async def _ask_orchestrator(
    query: str,
    history: list[dict] | None = None,
    guild_id: str | None = None,
    user_id: str | None = None,
) -> dict:
    """POST to the RAG orchestrator's /ask endpoint.

    `history` is an optional list of `{role, content}` chat turns in
    chronological order. The orchestrator may not yet support `history` —
    if it returns 422 we log once and retry the call without `history`,
    so the bot degrades gracefully to single-turn behaviour.

    `guild_id` / `user_id` identify the caller for the orchestrator's
    rate-limit gate (plan 14). Forwarded as-is when present; omitted
    when None so older orchestrators that don't know the fields stay
    happy.

    Returns the parsed JSON dict (`{"answer": str, "citations": [...]}`).
    Raises:
    - `RateLimitedError` on HTTP 429 (caller should surface the
      friendly retry message via `_format_rate_limited_message`).
    - `httpx.HTTPStatusError` on any other 4xx/5xx after the documented
      422+history fallback.
    """
    global _history_unsupported_logged

    body: dict = {"query": query, "repo": REPO}
    if history:
        body["history"] = history
    if guild_id is not None:
        body["guild_id"] = guild_id
    if user_id is not None:
        body["user_id"] = user_id

    async with httpx.AsyncClient(timeout=ASK_TIMEOUT_SECS) as h:
        r = await h.post(f"{RAG_URL}/ask", json=body)
        # Rate limit first — we do NOT retry and we do NOT fall back to
        # "no history". 429 is a positive signal from the orchestrator
        # that the user should wait; converting it into a silent retry
        # would defeat the whole point of the cap.
        if r.status_code == 429:
            try:
                err_body = r.json()
            except Exception:
                err_body = {}
            retry_after = int(err_body.get("retry_after") or 60)
            reason = str(err_body.get("error") or "rate_limited")
            raise RateLimitedError(retry_after=retry_after, reason=reason)
        # 409 "model not set" — distinct from rate limit / generic error.
        # The retry-without-history fallback below is NOT triggered here:
        # re-sending won't make the model materialise.
        if r.status_code == 409:
            raise ModelNotSetError()
        if r.status_code == 422 and history:
            # Most likely cause: orchestrator hasn't shipped the history
            # field yet. Log once at INFO, then retry single-turn so the
            # user still gets an answer.
            if not _history_unsupported_logged:
                log.info(
                    "rag /ask returned 422 with history payload — "
                    "falling back to single-turn (will not warn again)"
                )
                _history_unsupported_logged = True
            retry_body = {"query": query, "repo": REPO}
            if guild_id is not None:
                retry_body["guild_id"] = guild_id
            if user_id is not None:
                retry_body["user_id"] = user_id
            r = await h.post(f"{RAG_URL}/ask", json=retry_body)
            # A 429 on the retry still surfaces as RateLimitedError.
            if r.status_code == 429:
                try:
                    err_body = r.json()
                except Exception:
                    err_body = {}
                retry_after = int(err_body.get("retry_after") or 60)
                reason = str(err_body.get("error") or "rate_limited")
                raise RateLimitedError(retry_after=retry_after, reason=reason)
        r.raise_for_status()
        return r.json()


@tree.command(name="ask", description="Ask the project knowledge base")
@app_commands.describe(
    query="Your question",
    single="Force single-turn — don't open a thread",
)
async def ask(
    interaction: discord.Interaction,
    query: str,
    single: bool = False,
):
    if GUILD_ALLOWLIST and interaction.guild_id not in GUILD_ALLOWLIST:
        await interaction.response.send_message(
            "This bot isn't enabled in this server.", ephemeral=True
        )
        return

    query_id = str(uuid.uuid4())
    log.info(
        "bot ask",
        extra={
            "event": "bot.ask",
            "query_id": query_id,
            "guild_id": interaction.guild_id,
            "repo": REPO,
            "single": single,
        },
    )

    await interaction.response.defer(thinking=True)
    try:
        data = await _ask_orchestrator(
            query,
            guild_id=str(interaction.guild_id) if interaction.guild_id is not None else None,
            user_id=str(interaction.user.id),
        )
    except RateLimitedError as rle:
        log.info(
            "rag rate limited",
            extra={
                "event": "bot.response",
                "query_id": query_id,
                "guild_id": interaction.guild_id,
                "repo": REPO,
                "response_chars": 0,
                "outcome": "rate_limited",
                "reason": rle.reason,
                "retry_after": rle.retry_after,
            },
        )
        await interaction.followup.send(
            _format_rate_limited_message(rle), ephemeral=True,
        )
        return
    except ModelNotSetError:
        log.info(
            "rag model not set",
            extra={
                "event": "bot.response",
                "query_id": query_id,
                "guild_id": interaction.guild_id,
                "repo": REPO,
                "response_chars": 0,
                "outcome": "model_not_set",
            },
        )
        await interaction.followup.send(_MODEL_NOT_SET_MESSAGE, ephemeral=True)
        return
    except Exception:
        log.exception(
            "rag call failed",
            extra={
                "event": "bot.response",
                "query_id": query_id,
                "guild_id": interaction.guild_id,
                "repo": REPO,
                "response_chars": 0,
                "outcome": "error",
            },
        )
        await interaction.followup.send(
            "Something went wrong reaching the knowledge base. Try again shortly."
        )
        return

    formatted = _format(data["answer"], data.get("citations", []))
    log.info(
        "bot response",
        extra={
            "event": "bot.response",
            "query_id": query_id,
            "guild_id": interaction.guild_id,
            "repo": REPO,
            "response_chars": len(formatted),
            "outcome": "ok" if data.get("citations") else "empty",
        },
    )

    # Single-turn opt-out path: behaves exactly like the original bot.
    if single:
        await interaction.followup.send(formatted)
        return

    # Default: post the answer, then spin up a thread off the response so
    # the user can keep asking follow-ups in-place.
    answer_msg = await interaction.followup.send(formatted, wait=True)
    try:
        thread_name = (query or "follow-up").strip()[:THREAD_NAME_LIMIT] or "follow-up"
        # `followup.send` returns a WebhookMessage which — on some
        # discord.py versions — does not carry guild context, so calling
        # `.create_thread()` on it raises "message does not have guild
        # info attached". Re-fetching the message through the channel
        # returns a full Message with the guild attached.
        starter: discord.Message
        if interaction.channel is not None and hasattr(interaction.channel, "fetch_message"):
            try:
                starter = await interaction.channel.fetch_message(answer_msg.id)
            except discord.HTTPException:
                starter = answer_msg  # fall through; may still work
        else:
            starter = answer_msg
        await starter.create_thread(
            name=thread_name,
            auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
        )
    except (discord.HTTPException, ValueError):
        # Thread creation can fail (DMs, missing perms, non-guild
        # channel). The user still has the answer; log and move on.
        log.exception("failed to create thread for /ask follow-ups")


@client.event
async def on_message(message: discord.Message) -> None:
    """Pick up follow-up questions inside threads the bot started."""
    # Ignore the bot's own messages (and other bots/webhooks).
    if message.author.bot:
        return
    # Only handle threads (i.e. the channel must be a Thread).
    if not isinstance(message.channel, discord.Thread):
        return
    # Guild allowlist applies to follow-ups too.
    if GUILD_ALLOWLIST and message.guild and message.guild.id not in GUILD_ALLOWLIST:
        return

    thread = message.channel
    # The thread's starter message ID == the thread's own ID. We use that to
    # confirm "the bot started this thread": the parent message's author is
    # the bot.
    if not await _is_bot_thread(thread):
        return

    query_id = str(uuid.uuid4())
    log.info(
        "bot thread followup",
        extra={
            "event": "bot.thread_followup",
            "query_id": query_id,
            "guild_id": message.guild.id if message.guild else None,
            "repo": REPO,
        },
    )

    history = await _collect_thread_history(thread)
    try:
        data = await _ask_orchestrator(
            message.content,
            history=history,
            guild_id=str(message.guild.id) if message.guild else None,
            user_id=str(message.author.id),
        )
    except RateLimitedError as rle:
        log.info(
            "rag rate limited for thread follow-up",
            extra={
                "event": "bot.response",
                "query_id": query_id,
                "guild_id": message.guild.id if message.guild else None,
                "repo": REPO,
                "response_chars": 0,
                "outcome": "rate_limited",
                "reason": rle.reason,
                "retry_after": rle.retry_after,
            },
        )
        await thread.send(
            _format_rate_limited_message(rle),
            silent=True,
        )
        return
    except ModelNotSetError:
        log.info(
            "rag model not set for thread follow-up",
            extra={
                "event": "bot.response",
                "query_id": query_id,
                "guild_id": message.guild.id if message.guild else None,
                "repo": REPO,
                "response_chars": 0,
                "outcome": "model_not_set",
            },
        )
        await thread.send(_MODEL_NOT_SET_MESSAGE, silent=True)
        return
    except Exception:
        log.exception(
            "rag call failed for thread follow-up",
            extra={
                "event": "bot.response",
                "query_id": query_id,
                "guild_id": message.guild.id if message.guild else None,
                "repo": REPO,
                "response_chars": 0,
                "outcome": "error",
            },
        )
        await thread.send(
            "Something went wrong reaching the knowledge base. Try again shortly.",
            silent=True,
        )
        return

    formatted = _format(data["answer"], data.get("citations", []))
    log.info(
        "bot response",
        extra={
            "event": "bot.response",
            "query_id": query_id,
            "guild_id": message.guild.id if message.guild else None,
            "repo": REPO,
            "response_chars": len(formatted),
            "outcome": "ok" if data.get("citations") else "empty",
        },
    )
    await thread.send(
        formatted,
        silent=True,  # MessageFlags(suppress_notifications=True)
    )


async def _is_bot_thread(thread: "discord.Thread") -> bool:
    """Return True iff this thread's starter message was authored by us.

    Tries the cached `starter_message` first (free), then falls back to
    fetching the parent message from the parent channel using the fact
    that a thread's ID == its starter message's ID.
    """
    if client.user is None:
        return False
    starter = thread.starter_message
    if starter is not None:
        return starter.author == client.user

    parent = thread.parent
    if parent is None:
        return False
    try:
        msg = await parent.fetch_message(thread.id)
    except discord.HTTPException:
        return False
    return msg.author == client.user


# ---------------------------------------------------------------------------
# /model slash commands (plan 17)
# ---------------------------------------------------------------------------
#
# Read-only commands (`list`, `current`) are open to anyone in the guild.
# `set` is gated on Discord's built-in Manage Server permission — no custom
# roles or user allowlists for now.

import time as _time

_MODELS_BOT_CACHE_TTL = 30.0
_models_bot_cache: dict[str, object] = {"ids": [], "fetched_at": -1e18}


async def _fetch_models(url: str) -> list[str]:
    """Return model IDs from the orchestrator's /models, 30s bot-side cache."""
    now = _time.monotonic()
    if (now - float(_models_bot_cache["fetched_at"])) < _MODELS_BOT_CACHE_TTL:
        return list(_models_bot_cache["ids"])  # type: ignore[arg-type]
    async with httpx.AsyncClient(timeout=10) as h:
        r = await h.get(f"{url}/models")
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
    _models_bot_cache["ids"] = ids
    _models_bot_cache["fetched_at"] = now
    return list(ids)


async def _fetch_current(url: str, repo: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as h:
        r = await h.get(f"{url}/settings", params={"repo": repo})
        r.raise_for_status()
        return r.json()


async def _set_model(url: str, repo: str, name: str, user_id: str) -> tuple[int, dict]:
    """Return (status_code, json_body). 400 is surfaced upward, not raised."""
    async with httpx.AsyncClient(timeout=10) as h:
        r = await h.post(
            f"{url}/settings",
            json={"repo": repo, "chat_model": name, "updated_by": user_id},
        )
    try:
        body = r.json()
    except Exception:
        body = {"error": r.text[:500]}
    return r.status_code, body


model_group = app_commands.Group(
    name="model",
    description="Inspect or change the active chat model",
)


@model_group.command(name="list", description="List available chat models")
async def _model_list(interaction: discord.Interaction):
    if GUILD_ALLOWLIST and interaction.guild_id not in GUILD_ALLOWLIST:
        await interaction.response.send_message(
            "This bot isn't enabled in this server.", ephemeral=True,
        )
        return
    log.info(
        "bot model list",
        extra={
            "event": "bot.model_list",
            "guild_id": interaction.guild_id,
            "repo": REPO,
        },
    )
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        ids = await _fetch_models(RAG_URL)
    except Exception:
        log.exception("/model list: fetch failed")
        await interaction.followup.send(
            "Couldn't reach the model list right now. Try again shortly.",
            ephemeral=True,
        )
        return
    if not ids:
        await interaction.followup.send(
            "No models exposed by the LiteLLM backend.", ephemeral=True,
        )
        return
    shown = ids[:25]
    body = "```\n" + "\n".join(shown) + "\n```"
    if len(ids) > 25:
        body += f"_showing 25 of {len(ids)} — use `/model set` with any id above_"
    await interaction.followup.send(body, ephemeral=True)


@model_group.command(name="current", description="Show the active chat model for this instance")
async def _model_current(interaction: discord.Interaction):
    if GUILD_ALLOWLIST and interaction.guild_id not in GUILD_ALLOWLIST:
        await interaction.response.send_message(
            "This bot isn't enabled in this server.", ephemeral=True,
        )
        return
    log.info(
        "bot model current",
        extra={
            "event": "bot.model_current",
            "guild_id": interaction.guild_id,
            "repo": REPO,
        },
    )
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data = await _fetch_current(RAG_URL, REPO)
    except Exception:
        log.exception("/model current: fetch failed")
        await interaction.followup.send(
            "Couldn't reach the orchestrator right now. Try again shortly.",
            ephemeral=True,
        )
        return
    chat_model = data.get("chat_model")
    if not chat_model:
        # No chart default exists (plan 17). /ask will refuse until a
        # Manage-Server user runs /model set.
        await interaction.followup.send(
            "No chat model is configured for this instance yet.\n"
            "A server admin can run `/model set <name>` "
            "(use `/model list` to see the options).",
            ephemeral=True,
        )
        return
    when = data.get("updated_at") or "unknown"
    who_raw = data.get("updated_by")
    # Discord snowflakes are 17–20-digit integers. Mention when it looks
    # like one; show literal string otherwise.
    if who_raw and who_raw.isdigit() and 17 <= len(who_raw) <= 20:
        who = f"<@{who_raw}>"
    else:
        who = who_raw or "unknown"
    await interaction.followup.send(
        f"Active model: `{chat_model}`\nLast changed: {when} by {who}",
        ephemeral=True,
    )


async def _model_name_autocomplete(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    try:
        ids = await _fetch_models(RAG_URL)
    except Exception:
        return []
    # Case-insensitive prefix/substring match. Discord caps at 25 choices.
    q = current.lower()
    matches = [i for i in ids if q in i.lower()][:25]
    return [app_commands.Choice(name=i, value=i) for i in matches]


@model_group.command(name="set", description="Set the active chat model (requires Manage Server)")
@app_commands.describe(name="Model id — use /model list to see what's available")
@app_commands.autocomplete(name=_model_name_autocomplete)
async def _model_set(interaction: discord.Interaction, name: str):
    if GUILD_ALLOWLIST and interaction.guild_id not in GUILD_ALLOWLIST:
        await interaction.response.send_message(
            "This bot isn't enabled in this server.", ephemeral=True,
        )
        return
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None or not perms.manage_guild:
        await interaction.response.send_message(
            "This command requires the **Manage Server** permission.",
            ephemeral=True,
        )
        return
    log.info(
        "bot model set",
        extra={
            "event": "bot.model_set",
            "guild_id": interaction.guild_id,
            "repo": REPO,
            "user_id": str(interaction.user.id),
            "model": name,
        },
    )
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        status, body = await _set_model(RAG_URL, REPO, name, str(interaction.user.id))
    except Exception:
        log.exception("/model set: request failed")
        await interaction.followup.send(
            "Couldn't reach the orchestrator right now. Try again shortly.",
            ephemeral=True,
        )
        return
    if status == 400:
        # 4xx validation errors ARE actionable (bad model name, etc.) —
        # surface verbatim so the user can self-correct.
        err = body.get("error", "unknown error")
        available = body.get("available") or []
        sample = ", ".join(f"`{a}`" for a in available[:10])
        more = f" (+{len(available) - 10} more)" if len(available) > 10 else ""
        await interaction.followup.send(
            f"{err}\nAvailable: {sample}{more}" if sample else err,
            ephemeral=True,
        )
        return
    if status >= 500:
        # 5xx is a backend fault. Log the raw body for us; give the user
        # something friendly instead of a psycopg traceback in Discord.
        log.error(
            "rag POST /settings returned %s",
            status,
            extra={"event": "bot.model_set_backend_error", "status": status, "body": body},
        )
        await interaction.followup.send(
            "Couldn't update the active model right now. Try again shortly.",
            ephemeral=True,
        )
        return
    if status >= 300:
        # Other 4xx (401/403/404 etc.) — show the sanitised error field, not the raw body.
        err = body.get("error", "request rejected")
        await interaction.followup.send(
            f"Couldn't update the model: {err}",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"Active chat model for this instance is now `{name}`.",
        ephemeral=True,
    )


tree.add_command(model_group)


@client.event
async def on_ready():
    log.info(
        "logged in as %s (repo=%s)" % (client.user, REPO),
        extra={"event": "bot.ready", "repo": REPO},
    )
    await tree.sync()


if __name__ == "__main__":
    client.run(TOKEN)
