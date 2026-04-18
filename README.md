# gitdoc

A Discord bot that answers questions about a software project by reading
that project's own code and docs. One bot per repo. Ask it anything — it
replies with a grounded answer and citations back to the files it used.

```
you: /ask how does the ingestion service decide which files to index?

bot: (in a new thread)

      Ingestion filters files by extension — code files in a fixed
      language map (Python, Go, TS/JS, Rust, Java, etc.) and doc files
      (`.md`, `.rst`, `.txt`, `.adoc`, `.mdx`). Anything else is skipped.
      It also skips vendored directories (`node_modules`, `.git`,
      `vendor`, …) and files over 500 KB.

      Sources
      - `services/ingestion/ingest.py` @ `9b83072`
```

Reply in the thread and the bot treats it as a follow-up — the prior
turns are included in the next answer's context.

## Commands

| Command | What it does |
|---|---|
| `/ask <question>` | Answer the question with citations; opens a thread for follow-ups. Add `single:true` to skip the thread. |
| *(reply in thread)* | Continue the conversation; prior turns are included. |
| `/model list` | Show the chat models available from LiteLLM. |
| `/model current` | Show which model this instance is using. |
| `/model set <name>` | Switch this instance to a different chat model. Requires **Manage Server**. |

## Docs

- **Start here:** [`docs/architecture.md`](docs/architecture.md) — how
  the pieces fit together.
- **Deploying a new instance:** [`deploy/DEPLOY.md`](deploy/DEPLOY.md) +
  [`docs/discord-setup.md`](docs/discord-setup.md).
- **Retrieval internals** (hybrid search, reranker, cache, rate limits):
  [`docs/retrieval.md`](docs/retrieval.md).
- **Webhook-driven ingestion:** [`docs/webhooks.md`](docs/webhooks.md).
- **Metrics & logs:** [`docs/observability.md`](docs/observability.md).
- **Runtime model selection:** [`docs/models.md`](docs/models.md).
- **Secrets workflow:** [`deploy/SECRETS.md`](deploy/SECRETS.md).
- **Design decisions & open work:**
  [`implementation_plans/`](implementation_plans/).

## License

MIT — see [LICENSE](LICENSE).
