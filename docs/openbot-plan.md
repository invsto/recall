# Recall for OpenBot implementation plan

## Problem

OpenBot bots need to resume work from concise durable context and read shared project decisions. The existing local Recall index serves coding sessions and Markdown context banks. Its read-only OpenBot helper projects one operator-selected file. A shared service needs separate storage and authenticated bot and project scopes.

## Design and users

- A bot uses its own private checkpoint to resume a named task or project, even after a Recall restart.
- Concurrent bot sessions save against a revision and can safely retry an ambiguous write with the same request ID.
- Project members can propose a structured brief or a separately identified source-linked excerpt for review. Members read only accepted content, and they lose access when membership is revoked.
- A trusted operator can inspect proposals, approve or reject them, provision and rotate bot credentials, manage memberships, export Markdown, and apply retention.
- A deployment operator can check health and restore a backup without reactivating any old credentials.

Use the official Python MCP SDK with the optional `openbot` extra, HTTP Streamable transport at `/mcp`, stateless request handling, and JSON responses. OpenBot supplies a unique static `Authorization: Bearer …` header per bot. Recall hashes a generated 256-bit token in its store and derives identity from that digest on every request. Tool calls recheck the credential inside the same database transaction as authorization and access. This also orders revocation and membership changes against in-flight writes. The operator interface is a local CLI scoped by database filesystem permissions.

Bind to loopback by default, with host allowlisting and browser Origin checks. For other hosts, terminate TLS at a trusted proxy and restrict network access to the proxy listener. Run Recall with a separate OS identity or in a container with an exclusive private volume. On the OpenBot side, ensure agent shells cannot read credentials for other agents or host/provider-wide credentials. The OpenBot per-agent access list controls configuration delivery. [OpenBot integration architecture](https://github.com/nightly-labs/openbot/blob/main/docs/architecture/integrations.md#who-gets-each-mcp-server).

## Interfaces

| Interface | Contract |
| --- | --- |
| `recall_checkpoint_get(scope, key, revision?)` | Returns the caller's current checkpoint, or a retained historical revision, with its revision and update time. Scope is `task` or `project`. A fresh slot has revision 0. A deleted slot returns a new revision and a null checkpoint. |
| `recall_checkpoint_save(scope, key, expected_revision, request_id, checkpoint)` | Replaces the whole checkpoint when the revision matches. Value contains a summary, decisions, questions, actions, and source references. Exact retries return the original acknowledgement while its target revision is retained. Older retained retry receipts cannot overwrite later values. |
| `recall_brief_propose(project, key, expected_revision, request_id, markdown)` | A current member submits a structured Recall v1 or v2 document for local operator review. The requested revision is the current accepted revision for that project/key and content type. |
| `recall_excerpt_propose(project, key, expected_revision, request_id, excerpt, source_ref)` | A current member submits a bounded, explicitly selected excerpt and required HTTPS source reference. An excerpt has its own revision chain. |
| `recall_brief_get(project, key)` and `recall_excerpt_get(project, key)` | A member reads the current accepted item with its author, reviewer, review time, and revision provenance. |
| `recall_project_search(query, project?, limit?)` | Returns references to matching current checkpoints owned by the caller and accepted items in projects where the caller is still a member. An optional project limits the search to that project's accepted material and the caller's project checkpoint of that key. Fetch specific items for content. |
| `recall openbot --db PATH …` | Local operator CLI for init, serve, health, bot credentials, projects and memberships, proposals/reviews, export, delete, prune, audit, backup, and restore. |
| `GET /health` | Returns readiness status without exposing storage paths, identities, or content. |

Typed tool arguments reject unknown fields and out-of-budget input. Limit requests to 128 KiB, Markdown to 48 KiB, excerpt text to 2 KiB, checkpoint content to 8 KiB, and search to at most 20 results. Reject instead of truncating meaningful text. Tool outputs use a structured `ok` flag, a stable error code, and a current revision on conflicts where the caller is authorized to know it. A retry key belongs to one principal and operation, and binding it to a different payload returns a conflict.

Store checkpoints and their revision history, proposals, publications, membership, credential digests, retries, and content-free audit metadata in a separate SQLite database with a versioned schema. Store accepted revisions with author and decision provenance. Search applies ownership/membership predicates inside SQL. Use ordinary SQLite indexes on scoped row keys and search current content in place, so there are no text index copies to expire. Use short transactions and a revision counter per slot. Preserve a content-free marker when a slot is deleted so an old create or update cannot recreate removed content. Keep retry receipts for as long as their target revisions are retained. Keep credential material, request bodies, and content out of logs.

Validate project briefs with the existing Markdown contract and export exact accepted Markdown bytes. The existing Markdown bank, CLI/index, and projected read-only helper stay compatible. Anything fetched through Recall is project data. The service does not fetch evidence links or discover/import transcripts.

## Slices and acceptance

| Slice | Depends on | Acceptance through the public CLI and HTTP MCP seams |
| --- | --- | --- |
| 1. Provision and resume | None | Operator initializes a fresh store, issues three independent credentials, and starts the service. A bot saves structured context and reads it after restart. A second bot and outsider cannot read it. Missing/invalid headers are rejected; identity metadata cannot change the caller. |
| 2. Safe writes and credential lifecycle | 1 | Simultaneous saves using one expected revision produce one winner. Exact retry is stable, mismatched retry is rejected, and rotation/revocation rejects the former credential on subsequent requests. |
| 3. Review scoped project material | 1, 2 | Two members can see an approved Markdown brief and separately approved excerpt; pending/rejected proposals and an outsider cannot see them. Export matches published Markdown bytes. A stale approval and a proposal from a removed member cannot publish. |
| 4. Search and storage lifecycle | 3 | Queries expose only current authorized rows. Membership removal immediately removes shared reads/searches. Delete/prune remove live content and retry references. A consistent backup restores into a fresh store and needs new credentials. |
| 5. Repeatable pilot | 1–4 | CI runs the official SDK integration tests. A portable skill and operator runbook describe setup, outage/conflict handling, synthetic rollout, and rollback for 23 bots. |

Use synthetic-only inputs and real client calls to a local service subprocess. Use the local CLI for provisioning, review, search lifecycle setup, and backup/restore. Include the full existing Python suite and Pi checks for compatibility.

## Pilot and rollout

1. Run two synthetic members and a third outsider against a separate temporary store. Prove restart, write conflicts, review, source links, membership revocation, credential rotation, retention, and restore before configuring OpenBot.
2. Obtain operator approval for an isolated OpenBot pilot. Configure one server entry per pilot agent with an agent-specific token. Check on the actual host that each provider session and agent shell can access only its credential, and that the SQLite volume, backups, and operator tools are inaccessible from bot runtimes. Repeat the protocol checks using the real OpenBot provider path. Use exact host/Origin allowlisting and HTTPS for non-loopback traffic. Record membership and retention decisions before adding any approved real context.
3. If the operator approves the pilot, roll out to 3, then 7, then the remaining 13 bots. After each batch check discovery, successful save/read, unauthorized denials, revocation, error rate, and backup recovery. Stop at the first isolation failure. Repeat for each provider/runtime configuration.
4. Roll back by disabling the Recall integration entries, revoking affected credentials, and stopping the listener if necessary. Keep the store isolated for diagnosis. Restore only into a fresh isolated path, reconcile deleted data and memberships against authoritative records, and issue new credentials before reconnecting. Existing Recall commands remain available throughout.

Transcript import requires its own authorization after checking an actual OpenBot export route, user approval, retention, and deletion behavior. Live OpenBot configuration, rollout, and private data are outside this implementation.
