import logging
import os

import discord
import httpx
from discord import app_commands

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("gitdoc.bot")

RAG_URL = os.environ["RAG_ORCHESTRATOR_URL"]
REPO    = os.environ["TARGET_REPO"]
TOKEN   = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ALLOWLIST = {
    int(g) for g in os.environ.get("GUILD_ALLOWLIST", "").split(",") if g.strip()
}

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


def _format(answer: str, citations: list[dict]) -> str:
    cites = "\n".join(
        f"- `{c['path']}` @ `{c['commit_sha'][:7]}`" for c in citations
    ) or "_no sources_"
    msg = f"{answer}\n\n**Sources**\n{cites}"
    # Discord hard-caps messages at 2000 chars.
    return msg if len(msg) <= 1990 else msg[:1987] + "..."


@tree.command(name="ask", description="Ask the project knowledge base")
@app_commands.describe(query="Your question")
async def ask(interaction: discord.Interaction, query: str):
    if GUILD_ALLOWLIST and interaction.guild_id not in GUILD_ALLOWLIST:
        await interaction.response.send_message(
            "This bot isn't enabled in this server.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    try:
        async with httpx.AsyncClient(timeout=60) as h:
            r = await h.post(
                f"{RAG_URL}/ask",
                json={"query": query, "repo": REPO},
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        log.exception("rag call failed")
        await interaction.followup.send(
            "Something went wrong reaching the knowledge base. Try again shortly."
        )
        return

    await interaction.followup.send(_format(data["answer"], data.get("citations", [])))


@client.event
async def on_ready():
    log.info("logged in as %s (repo=%s)", client.user, REPO)
    await tree.sync()


if __name__ == "__main__":
    client.run(TOKEN)
