# Discord application + bot setup

Run this once per instance. It creates the Discord-side identity that the
`discord-bot` pod will authenticate as, plus the invite URL and guild ID
you feed to the cluster deploy.

The bot pod and the Discord application are linked by a single thing: the
bot token. Everything below is about creating that token and granting it
the right permissions.

## 1. Create the application

1. Go to https://discord.com/developers/applications
2. **New Application** → name it (e.g. `gitdoc-<slug>`; the name appears
   to server members as the bot's display name by default — you can
   override it later in the Bot tab).
3. Accept the Discord Developer ToS.

## 2. Make the bot private

By default Discord marks new bots as "Public", meaning anyone with the
invite URL can add them to their server. You almost certainly want the
opposite: only you, the owner, can invite it.

1. **Bot** tab in the left sidebar
2. Scroll to **Public Bot** and toggle it **OFF**
3. **Save Changes**

**If the Public Bot toggle is greyed out**, the newer Installation tab is
overriding it. Fix:

1. **Installation** tab (left sidebar)
2. **Install Link** → change to **None** (not "Discord Provided Link")
3. **Installation Contexts** → tick **Guild Install**, untick **User Install**
4. **Save Changes**
5. Back to **Bot** tab → Public Bot now toggles normally. Flip it OFF and save.

## 3. Enable the Message Content Intent

The bot reads message bodies of follow-up replies inside threads it
started. That requires a *privileged* Discord intent that is off by
default.

- Bot tab → **Privileged Gateway Intents**
- ✅ **MESSAGE CONTENT INTENT** — toggle ON
- Leave **Presence Intent** and **Server Members Intent** OFF (we don't
  use them)
- **Save Changes**

Without this toggle the bot will log in fine, `/ask` will work, but
replies inside threads will be silently ignored (the bot sees empty
`.content` strings). It's the single most common "thread follow-ups
aren't working" cause.

## 4. Get the bot token

- Bot tab → **Reset Token** → **Yes, do it** → **Copy**

This is the `DISCORD_BOT_TOKEN` value that goes into the sealed Secret
the cluster agent builds. Treat it like a password:

- Don't commit it to any repo (even private)
- Don't paste it into chat
- If it ever leaks, hit **Reset Token** again — the old one is
  invalidated immediately

## 5. Generate the invite URL

- **OAuth2** tab → **URL Generator**
- **Scopes** (tick both):
  - `bot`
  - `applications.commands`
- **Bot Permissions** (tick these):
  - `View Channels`
  - `Send Messages`
  - `Send Messages in Threads`
  - `Create Public Threads`
  - `Read Message History`
  - `Embed Links`
  - `Use Slash Commands`
- Copy the URL generated at the bottom of the page.

Because you flipped Public Bot OFF in step 2, this invite URL only works
when you (the app owner) click it. Nobody else can use it to add the bot
anywhere.

## 6. Invite the bot to your server

- Paste the URL into your browser → select the target server → **Authorize**
- Solve the captcha if prompted

The bot will appear in the member list as **offline**. It stays offline
until the `discord-bot` pod is deployed in your cluster, reads the token
from the Secret, and completes the login handshake. At that point it
flips to **online** — usually within a few seconds of the pod reaching
Ready.

## 7. Get the guild ID

The chart's `discordBot.guildAllowlist` value gates the `/ask` command
to specific servers (it's a safety net — even if someone re-invites the
bot somewhere unexpected, it won't respond there).

- In the Discord client: **User Settings → Advanced → Developer Mode** (toggle ON, one-time)
- In the server list: right-click your server icon → **Copy Server ID**
- That 17–20 digit snowflake is the `<GUILD_ID>` you hand to the cluster
  agent. Multiple guilds go comma-separated.

## Summary of values you collected

By the end you should have three values to feed into the cluster deploy:

| Value | Goes into | Where from |
|---|---|---|
| `DISCORD_BOT_TOKEN` | sealed Secret `gitdoc-<slug>` | step 4 |
| Invite URL | your browser, once, to add the bot to your server | step 5 |
| `GUILD_ID` | chart values override (`discordBot.guildAllowlist`) | step 7 |

## After deploy

Once the pod is online in your server:

- Type `/ask what does this project do` in any channel the bot has
  `View Channels` on. Expect an answer + citations in a new thread.
- Reply in the thread — the bot treats follow-ups as part of the same
  conversation.
- `/model list` shows what LiteLLM is exposing.
- `/model current` shows the active model (or "using the chart default"
  if you haven't set one).
- `/model set <name>` changes it (requires **Manage Server** in Discord).

If slash commands don't show up in autocomplete, they're still
registering — wait a minute. If they never appear, the invite URL was
missing the `applications.commands` scope — re-generate it from step 5
and re-invite.

## Deleting an instance

Two cleanups on the Discord side when decommissioning a bot:

1. Kick the bot from every server it's in (server settings → Integrations → the app → Remove).
2. If you want the application name freed up, delete the app in the dev portal (Applications list → the app → Delete App at the bottom of General Information).

The cluster side is handled by `helm uninstall` + the revoke-instance.sql
teardown — see `db/provision/README.md`.
