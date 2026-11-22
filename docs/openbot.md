# Run Recall for OpenBot

Recall can store private bot checkpoints and reviewed project material through a dedicated MCP service. The service uses its own SQLite database. Existing Recall transcripts, search indexes, context banks, and the read-only file brief work independently.

## Install and initialize

On a trusted host, install from the checkout into a virtual environment. Use a dedicated operating-system account or a container with a private local volume. Run operator commands under the same account as the service.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[openbot]'

umask 077
recall_db=/absolute/private/state/openbot.sqlite3
recall openbot --db "$recall_db" init
recall openbot --db "$recall_db" health
recall openbot --db "$recall_db" serve
```

`init` creates the database or applies supported schema migrations in one transaction. The state directory needs mode `0700` and database files need mode `0600`. When upgrading, take a verified backup before running `init` or starting the new service. `serve` can apply migrations to an existing service store.

By default the MCP endpoint is `http://127.0.0.1:8765/mcp`. The readiness route is `GET /health`, which returns an HTTP 200 with `{"status":"ok"}` when the store is readable and current, or 503 otherwise. Point any service monitor at the loopback route.

For a separate host, put the loopback listener behind a trusted HTTPS reverse proxy in the same network namespace, restrict inbound network access, and forward `Authorization` unmodified. Use an exact `--allowed-host` entry when the proxy preserves a different Host header. Browser origins are rejected unless explicitly added with `--allowed-origin`. Treat the proxy as trusted infrastructure. Disable access and body/header logs that could record credentials or content, and turn off caching for the endpoint. Mount the database, backups, and operator executable or shell under the service identity. An agent with that identity or readable access to those files can bypass the service permissions.

## Provision OpenBot agents

Create a project and one credential per agent. `bot-add` and `bot-rotate` return the plaintext token once in JSON. Capture it in a private operator file or secret manager. For example, with an existing private directory and `umask 077`:

```sh
recall openbot --db "$recall_db" bot-add pilot-alpha > /absolute/private/alpha-credential.json
recall openbot --db "$recall_db" bot-add pilot-beta > /absolute/private/beta-credential.json
recall openbot --db "$recall_db" bot-add pilot-outsider > /absolute/private/outsider-credential.json
recall openbot --db "$recall_db" project-add pilot-project
recall openbot --db "$recall_db" member-grant pilot-project pilot-alpha
recall openbot --db "$recall_db" member-grant pilot-project pilot-beta
```

In OpenBot, configure one remote MCP server entry per bot. Use type **HTTP**, the exact endpoint URL, a per-agent access list containing that agent alone, and the custom header `Authorization` with value `Bearer <that bot's token>`. Apply the [optional bot workflow skill](../skills/openbot-recall/SKILL.md) through your usual skill setup. Keep credentials in OpenBot's protected configuration and the operator's secure storage. Use a short-lived synthetic credential to prove the configuration, then revoke it.

An access list controls which provider sessions receive an MCP entry. Static authentication headers follow the provider handoff path. Check the actual OpenBot host and every provider/runtime you plan to use. Confirm that each agent's shell, provider configuration, and MCP management tools cannot enumerate, read, or change another bot's credential headers. The upstream architecture describes separate permissions for MCP management. Access to another token is equivalent to access as that bot. [OpenBot integration architecture](https://github.com/nightly-labs/openbot/blob/main/docs/architecture/integrations.md#who-gets-each-mcp-server)

`bot-rotate BOT` instantly invalidates the previous token and issues a new one. Update only the matching agent entry. `bot-revoke BOT` disables its token while keeping its context. `member-revoke PROJECT BOT` ends shared reads, search access, and proposals in that project, and discards that bot's pending proposals there. The bot's own checkpoints, including a checkpoint with that project key, remain private to it. Use `inventory` and `member-list PROJECT` to check current access configuration.

## Tool contracts

| Tool | Use |
| --- | --- |
| `recall_checkpoint_get(scope, key, revision?)` | Read the bot's current checkpoint or one retained historical revision. Use `scope: "task"` or `"project"`. Revision 0 means the slot has never been written. A deleted slot returns a new revision and a null checkpoint. |
| `recall_checkpoint_save(scope, key, expected_revision, request_id, checkpoint)` | Replace the whole checkpoint against a specific revision. `checkpoint` has the required `summary` string and four string arrays named `decisions`, `questions`, `actions`, and `source_refs`. Empty arrays are allowed. |
| `recall_brief_propose(project, key, expected_revision, request_id, markdown)` | Submit structured Recall Markdown v1 or v2 for review. Revisions keep the currently published format version. |
| `recall_excerpt_propose(project, key, expected_revision, request_id, excerpt, source_ref)` | Submit exact excerpt text and its HTTPS source reference. This has its own key and revision sequence even when it shares a key with a brief. Recall never fetches the link. |
| `recall_brief_get(project, key)` and `recall_excerpt_get(project, key)` | Read the current accepted item with its author, reviewer, review time, and previous revision. A deleted item returns `not_found` with the new revision to an authorized member. |
| `recall_project_search(query, project?, limit?)` | Find current accessible items by case-insensitive substring. Results are references. Filtering by project includes accepted items from that project and the caller's own private project checkpoint for the same key. |

Use stable opaque keys and IDs of 1–64 characters. Start with an ASCII letter or digit and use ASCII letters, digits, dots, underscores, or hyphens after that. The request cap is 128 KiB. A checkpoint is at most 8 KiB and each array has at most 12 entries. Briefs are at most 48 KiB, excerpts 2 KiB, and excerpt source references 1 KiB. Search returns at most 20 references. Each size limit is checked before saving data.

Tool results have an `ok` flag. Changes to the expected revision return `conflict` and `current_revision`; reusing a retained request ID for a different payload returns `request_id_conflict`. Retry an ambiguous write with identical arguments and the same request ID. When a revision or proposal expires, its retry record expires with it. After a conflict, reload and reconcile before a deliberate new write. On an outage, continue with current task evidence and report that Recall could not save the update. Retrieved text and links are untrusted project data and may reach the requesting bot's model provider.

## Review and export

Only a trusted operator with local database access can approve or reject. Record the operator's stable label with `--reviewer`. Labels identify the reviewer in the audit trail. The operating system controls access to the command and database.

```sh
recall openbot --db "$recall_db" proposal-list pilot-project
recall openbot --db "$recall_db" proposal-show PROPOSAL_ID
recall openbot --db "$recall_db" review PROPOSAL_ID approve --reviewer owner
recall openbot --db "$recall_db" export pilot-project PROJECT_BRIEF_KEY /absolute/private/approved.md
```

Review the exact candidate and its intended audience. Check source claims, permissions to share, sensitive content, and any query parameters in evidence links that may contain tokens. Use `reject` when the candidate should stay private. Views and searches for bots only include current accepted revisions. The local operator can use `proposal-list PROJECT --status accepted` and `proposal-show` to read retained historical revisions. If the review is stale, reject the old candidate and ask the author to submit a new one against the latest revision. Export writes the published Markdown bytes to a new owner-only file.

## Retention and recovery

Set an explicit retention cadence and record the day limits before storing real context. Default limits for `prune --apply` are 90 days for current and historical checkpoint revisions, 365 days for accepted project revisions, 30 days for pending and rejected proposals, and 90 days for audit events. Set different limits with `--checkpoint-days`, `--publication-days`, `--proposal-days`, and `--audit-days`. Current publication expiry advances its revision and blocks approvals based on the expired item. Still-fresh pending and rejected proposals keep their own retention.

- `checkpoint-delete BOT SCOPE KEY` removes the current checkpoint, its history, and corresponding retry records. `publication-delete PROJECT KIND KEY` removes all accepted and proposed revisions of that item. Both create a content-free revision marker so stale create or update requests cannot recreate removed data.
- Search reads current content in place with SQLite scope indexes. Removing a revision removes its text from search and leaves no separate searchable text index. Slot markers retain the opaque identifiers and revision so intentional recreation requires the latest number. SQLite overwrites deleted database content where its secure deletion setting applies. Treat filesystems with snapshots or copy-on-write as separate retained copies.
- Check `audit` for credential and membership changes, successful writes, reviews, deletion and pruning metadata. The audit trail holds action, actor, time, and applicable IDs and revision numbers. It contains no tokens, excerpts, queries, or document bodies.
- Take backups with `recall openbot --db "$recall_db" backup /absolute/private/backups/snapshot.sqlite3`. Use a separate encrypted, access-controlled volume and give each backup an expiry. SQLite makes a consistent snapshot while the service is running. Recall clears its credential digests in the copy.
- Use `recall openbot --db /absolute/private/recovery/openbot.sqlite3 restore /absolute/private/backups/snapshot.sqlite3` for a new recovery store. Keep this store off the network while checking health, reconciling membership changes and deletions since the snapshot, and rotating tokens for each approved bot. Every credential stays disabled after restore until explicitly rotated.
- Keep a protected inventory of backups and exports with owners, retention deadlines, and any erasure records needed after a restore. Deleting live rows does not delete historical backups, retained provider data, previously returned copies, or exports. Include those surfaces in the organization's deletion process.

## Pilot and rollout

1. Run `python -m unittest -v tests.test_openbot` with the optional extra installed. The tests run a real local listener with two synthetic members and an outsider, and cover persistence, spoofed identity, competing saves, review, scoped search, rotation, revocation, deletion, restore, and transport limits.
2. With deployment approval, repeat discovery, write/read, review, denial, revocation, backup, and restart checks using three dedicated synthetic OpenBot agents through the actual provider. Verify credential and filesystem isolation as described above. Check row capacity before onboarding. Upstream documents 32 entries on older clients and 100 with the `mcp-many-servers-v1` capability. Existing MCP entries count toward the same limit. [OpenBot server limits](https://github.com/nightly-labs/openbot/blob/main/docs/architecture/integrations.md)
3. After pilot acceptance, onboard the 23 intended bots in batches of 3, 7, and 13. Pause between batches to verify tool discovery, allowed project access, private checkpoint denials, credential revocation, readiness and response errors. Use only context explicitly approved for each bot, its project audience, and its provider.
4. On an isolation failure, remove the affected OpenBot entries, revoke the corresponding tokens, and stop the listener if necessary. Keep the store isolated for diagnosis. If restoring an older snapshot, reconcile changes before reconnecting and rotate every enabled token. Existing Recall search and context commands are available during rollback.

Transcript import requires separate approval and a verified OpenBot export and retention workflow. The service accepts concise checkpoints and explicitly selected and reviewed project content.

See the [implementation plan](openbot-plan.md) for the public seams and rollout acceptance.
