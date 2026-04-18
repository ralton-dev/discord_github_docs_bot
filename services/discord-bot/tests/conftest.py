"""Pytest conftest for the discord-bot service tests.

`bot.py` reads three required env vars at import time
(`RAG_ORCHESTRATOR_URL`, `TARGET_REPO`, `DISCORD_BOT_TOKEN`) and constructs a
`discord.Client`. The client constructor does not open a gateway connection
until `.run()` is called, so instantiating it in-test with a dummy token is
safe.

We set the env vars *here* (before pytest imports the test modules) so
`import bot` inside tests never raises `KeyError`. This is the
conftest-monkeypatch approach called out in the task brief — no refactor of
`bot.py` required.
"""

from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("RAG_ORCHESTRATOR_URL", "http://rag.local")
os.environ.setdefault("TARGET_REPO", "test-repo")
os.environ.setdefault("DISCORD_BOT_TOKEN", "dummy-token")

_SERVICE_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
