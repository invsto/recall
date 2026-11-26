# Recall

**Local memory for AI agents, and a searchable archive of your coding-agent conversations.**

Recall has two parts, both local and both backed by SQLite:

- **Memory server.** An MCP server that gives every agent its own memory: save, look up, correct, and forget notes and facts. One shared token serves all agents; each call names its agent in a `bot` argument, and each agent's memory stays separate.
- **Conversation archive.** Indexes your Claude Code, Codex, OpenCode, Pi, and ChatGPT sessions so you can rediscover past work, resume sessions, and keep reusable context in plain Markdown.

Nothing leaves your machine unless you turn on an optional feature that says otherwise.

![Animated walkthrough of Recall search, Pi tools, and context banks](docs/recall-demo.gif)

## What Recall does

- Indexes conversations across Claude Code, Codex, OpenCode, Pi, and ChatGPT exports.
- Searches with fuzzy matching, regular expressions, or optional local semantic search.
- Browses recent conversations and resumes supported sessions.
- Preserves reusable project context as plain Markdown context banks.
- Runs an MCP memory server (`python -m recall_v2`) with separate memory per agent behind one shared token.
- Runs as a standalone Python CLI or as an extension inside Pi.

## Local by default

Recall reads local transcripts and indexes them with SQLite FTS5. Core indexing and search use only the Python standard library and do not send your conversations anywhere. Optional semantic search downloads and runs an embedding model locally.

The archive keeps its index at `~/.recall/recall.db` and context banks under `~/.recall/contexts/`. The memory server keeps its store wherever `--store` points (default `~/.recall/store.sqlite3`).

## Quick start

### Pi extension

Install the published [`recall-pi`](https://www.npmjs.com/package/recall-pi) package:

```bash
pi install npm:recall-pi
```

The extension adds the `/recall` dashboard and the `recall_search` and `recall_context` tools. In Pi, you can ask naturally:

```text
Search my recent sessions for the retry backoff change.
Attach my migration-safe-notsafe context before we continue.
Create a safe-notsafe context from source at ~/source/github/viggy28/safe-not-safe.
```

Source-aware context creation asks before bounded repository inspection, discloses which selected Pi provider/model receives source excerpts, and separately asks before saving. See the [usage and privacy guide](docs/usage.md#privacy).

### Standalone CLI

Core search has no required third-party dependencies:

```bash
python3 recall.py index
python3 recall.py search "retry backoff"
python3 recall.py recent
```

You can also install the `recall` command from this checkout. See the [usage guide](docs/usage.md) for setup and all available commands.

## Memory server

Install the package with the `v2` extra (it adds the official `mcp` SDK), create a store and a shared token, and start the server on loopback:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[v2]'            # or: pip install 'recallhq-<version>-py3-none-any.whl[v2]'
umask 077
python -m recall_v2 --store ~/.recall/store.sqlite3 init
python -m recall_v2 --store ~/.recall/store.sqlite3 principal add shared-service   # prints the token once
python -m recall_v2 --store ~/.recall/store.sqlite3 serve --port 8765               # 127.0.0.1 only
```

- `GET http://127.0.0.1:8765/health` returns 200 `{"status":"ok"}` when the store answers.
- `POST http://127.0.0.1:8765/mcp` is the streamable-HTTP MCP endpoint. Send `Authorization: Bearer <token>`; without a current token it returns 401.

**Shared token and the `bot` argument.** The principal named `shared-service` is the shared token. Every tool call made with it must include `bot`, the calling agent's name (1–64 letters, digits, dots, underscores, or hyphens). Recall keeps each `bot` name's memory separate: one agent's lookups never return another's private items. A call without `bot` is refused with `invalid_input`. A principal with its own token uses its own memory and cannot pass `bot`.

The bot name is supplied by the caller, not proven by the server: anyone holding the shared token can use any name. Keep the token in your MCP client's protected configuration only.

**Tools.** `memory_save`, `memory_lookup`, `memory_get`, `memory_history`, `memory_forget`, `memory_retract`, `memory_bundle`, `memory_pin`, `memory_unpin`. Retrieved text is stored data, never instructions.

**Operator commands** (local CLI, not exposed over MCP): `principal add|rotate|revoke|list`, `project`, `member`, `review`, `backup`, `restore`, `export`, `audit`, `doctor`, `maintain`. Run `python -m recall_v2 --help` for the full list. `migrate` imports a store from Recall 0.7 or earlier into a new file and never changes the source.

For a step-by-step agent-platform setup, see the [platform runbook](recall_adapters/platforms/openbot/openbot.md). <!-- neutral-core: allow -->

## Knowledge graphs

`recall graph` extracts named entities locally with a deterministic, dependency-free
heuristic and connects entities mentioned in the same message. JSON output contains
nodes, weighted co-occurrence edges, and references to the source session, message,
path, and transcript line. DOT output can be opened by Graphviz and other graph tools.

```bash
# D3/Cytoscape-friendly JSON on stdout
recall graph --max-nodes 75 --min-edge-weight 2 > graph.json

# Scope the graph and create a Graphviz artifact
recall graph --source pi --project recall --since 2026-01-01 \
  --entity-type organization --format dot --output graph.dot
dot -Tsvg graph.dot > graph.svg
```

Available scopes include `--since`, `--until`, `--source`, `--project`, and
`--entity-type`. Use `--max-nodes` and `--min-edge-weight` to keep dense graphs
readable. Entity extraction is deterministic and dependency-free, and recognizes a
small set of precise token classes rather than every capitalized word: `@people`,
`#topics`, issue references (`#123`, `gh-123`), domains, file paths, and a curated
technology gazetteer (Postgres, React, GraphQL, …). Add `--ner` to layer on spaCy
named-entity recognition for people, organizations, and places in prose:

```bash
pip install spacy && python -m spacy download en_core_web_sm
recall graph --ner
```

For an interactive explorer, render a self-contained HTML file (no server or
network needed — open it directly in a browser):

```bash
recall graph --format html --output graph.html
open graph.html
```

## Context banks

Context banks turn useful material from past conversations into reusable Markdown documents. New contexts have a short human-readable summary, current understanding, open questions, optional details, and evidence in one document. Updates maintain the summary and detail together through reviewed changes. Existing contexts are not automatically migrated. Create, review, update, and attach them to future Pi sessions without moving your project context to a hosted service.

```bash
# Discover matching indexed sessions locally, select evidence, then choose a focus.
recall context create streambed-internals

# Or select evidence and focus explicitly.
recall context create streambed-internals \
  --session 019fcf9a \
  --session 019fd039 \
  --focus "Architecture, code paths, invariants, and technical debt"

recall context show streambed-internals
recall context update streambed-internals "Record the latest implementation decisions"
```

Use `recall context create NAME --blank` for an intentional empty template. Model-backed creation separately reviews evidence transmission and the generated document before saving.

![Recall context update review](docs/context-review-controls.svg)

## Tests

```bash
pip install -e '.[v2]'
python3 -m unittest -v              # everything CI runs in Python, including tests/v2
python3 -m pytest tests/v2 -q       # the memory-server tests on their own (needs pytest)
python3 scripts/ci/neutral_core.py  # keeps product names out of the core
```

## Documentation

- [Architecture](docs/architecture.md)
- [Usage, installation, semantic search, and context banks](docs/usage.md)
- [Development](docs/development.md)
- [Release cadence and deployment](docs/releases.md)

Recall is MIT licensed.
