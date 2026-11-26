# Architecture

Recall is one Python package (`recallhq`) with two independent parts. They share no database.

## Memory server: `recall_v2`

```
MCP client ──HTTP──▶ recall_v2.mcp ──▶ recall_v2.api.Client ──▶ recall_v2.store ──▶ SQLite store
 (Bearer token)      (auth, bot arg)    (one principal's view)    (transactions)      (one file, 0600)
operator ──CLI──▶ recall_v2.cli ──▶ recall_v2.api.Operator ─┘
```

- `recall_v2.store` owns the only memory store: schema, transactions, token digests, receipts, the audit chain, and erasure.
- `recall_v2.api` is the library surface. `Client` acts for one principal; `Operator` holds the local admin commands. `contract` defines every tool's fields, so the library, CLI, and MCP server return identical envelopes.
- `recall_v2.mcp` serves the tools over streamable HTTP (loopback only) or stdio, plus `GET /health`. Under the `shared-service` token it reads the `bot` argument, keys that bot's principal to a credential derived from the shared token, and runs the call as that bot. Under any other token, `bot` is refused.
- `recall_v2.retrieval` does lookup and ranking (lexical, optional vectors) and builds token-budget bundles.
- `recall_v2.governance` and `recall_v2.lifecycle` handle review, conflicts, dedupe, expiry, retention, backup, restore, and export.
- `recall_v2.extraction` is opt-in candidate extraction from local sources; it never runs unless a source is added and its manifest acknowledged.
- `recall_v2.migrate` imports a store from Recall 0.7 or earlier into a new file.

Nothing runs on a timer. Retention and maintenance run when the operator calls `maintain`; scheduling belongs to whoever runs the server.

## Conversation archive: `recall.py` and `recall_core`

- `recall.py` is the `recall` command and the backend of the Pi extension in `extensions/recall`.
- `recall_core.ingestion` reads Claude Code, Codex, OpenCode, Pi, and ChatGPT transcripts; `recall_core.indexing` stores them in `~/.recall/recall.db` with FTS5; `recall_core.retrieval` searches them.
- `recall_core.context_*` create, validate, and review Markdown context banks under `~/.recall/contexts/`.

See [development](development.md) for details of the archive modules and the Pi extension.
