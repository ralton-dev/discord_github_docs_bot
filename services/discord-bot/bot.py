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


async def _ask_orchestrator(
    query: str,
    history: list[dict] | None = None,
) -> dict:
    """POST to the RAG orchestrator's /ask endpoint.

    `history` is an optional list of `{role, content}` chat turns in
    chronological order. The orchestrator may not yet support `history` —
    if it returns 422 we log once and retry the call without `history`,
    so the bot degrades gracefully to single-turn behaviour.

    Returns the parsed JSON dict (`{"answer": str, "citations": [...]}`).
    Raises on transport/HTTP errors other than the documented 422 fallback.
    """
    global _history_unsupported_logged

    body: dict = {"query": query, "repo": REPO}
    if history:
        body["history"] = history

    async with httpx.AsyncClient(timeout=60) as h:
        r = await h.post(f"{RAG_URL}/ask", json=body)
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
            r = await h.post(
                f"{RAG_URL}/ask",
                json={"query": query, "repo": REPO},
            )
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
        data = await _ask_orchestrator(query)
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
        await answer_msg.create_thread(
            name=thread_name,
            auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
        )
    except discord.HTTPException:
        # Thread creation can fail (DMs, missing perms). The user still has
        # the answer; just log and move on.
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
        data = await _ask_orchestrator(message.content, history=history)
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


@client.event
async def on_ready():
    log.info(
        "logged in as %s (repo=%s)" % (client.user, REPO),
        extra={"event": "bot.ready", "repo": REPO},
    )
    await tree.sync()


if __name__ == "__main__":
    client.run(TOKEN)
