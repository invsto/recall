"""Store schema (user_version 11) and its migrations.

Version 10 is the M1 layout. Version 11 adds what retrieval needs: an FTS5 vocabulary view for
application-side BM25 over the caller's visible set, FTS5 secure-delete, and a (kind, key) index for
fact-mode lookup. Every store is built by applying each step in order, so a fresh store and a migrated
version-10 store end up identical.
"""
from __future__ import annotations

APPLICATION_ID = 0x52434F42
USER_VERSION = 11

SCHEMA_V10 = """
CREATE TABLE principals (
    id TEXT NOT NULL PRIMARY KEY,
    token_digest BLOB UNIQUE CHECK (token_digest IS NULL OR length(token_digest) = 32),
    created_at INTEGER NOT NULL,
    revoked_at INTEGER
) STRICT;

CREATE TABLE projects (
    id TEXT NOT NULL PRIMARY KEY,
    created_at INTEGER NOT NULL
) STRICT;

CREATE TABLE memberships (
    principal_id TEXT NOT NULL REFERENCES principals(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    added_at INTEGER NOT NULL,
    PRIMARY KEY (principal_id, project_id)
) STRICT;

CREATE TABLE items (
    id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('note', 'fact', 'checkpoint', 'brief', 'excerpt')),
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('private', 'project')),
    owner_id TEXT NOT NULL REFERENCES principals(id),
    project_id TEXT REFERENCES projects(id),
    key TEXT,
    current_rev INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER,
    CHECK ((scope_kind = 'project') = (project_id IS NOT NULL)),
    CHECK (kind <> 'checkpoint' OR scope_kind = 'private'),
    CHECK (kind <> 'brief' OR scope_kind = 'project'),
    CHECK ((deleted_at IS NULL) = (key IS NOT NULL))
) STRICT;

CREATE TABLE item_revisions (
    item_id TEXT NOT NULL REFERENCES items(id),
    rev INTEGER NOT NULL CHECK (rev BETWEEN 1 AND 9999999),
    state TEXT NOT NULL CHECK (state IN ('candidate', 'accepted', 'rejected')),
    title TEXT,
    body TEXT,
    body_hash BLOB,
    meta_json TEXT,
    valid_from INTEGER NOT NULL,
    valid_to INTEGER CHECK (valid_to IS NULL OR valid_to > valid_from),
    actor_id TEXT NOT NULL,
    request_id TEXT,
    recorded_at INTEGER NOT NULL,
    accepted_at INTEGER,
    reviewed_by TEXT,
    reviewed_at INTEGER,
    review_note TEXT,
    CHECK ((state = 'accepted') = (accepted_at IS NOT NULL)),
    PRIMARY KEY (item_id, rev)
) STRICT;

CREATE TABLE fact_versions (
    item_id TEXT NOT NULL,
    rev INTEGER NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('assert', 'retract')),
    subject TEXT,
    predicate TEXT,
    value_json TEXT,
    supersedes_rev INTEGER,
    PRIMARY KEY (item_id, rev),
    FOREIGN KEY (item_id, rev) REFERENCES item_revisions(item_id, rev)
) STRICT;

CREATE TABLE sources (
    id TEXT NOT NULL PRIMARY KEY,
    adapter TEXT NOT NULL,
    uri TEXT,
    content_hash BLOB NOT NULL,
    captured_at INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('live', 'missing', 'deleted'))
) STRICT;

CREATE TABLE extraction_runs (
    id TEXT NOT NULL PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    model_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash BLOB NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed', 'cancelled', 'budget_stopped'))
) STRICT;

CREATE TABLE provenance (
    item_id TEXT NOT NULL,
    rev INTEGER NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(id),
    chunk_hash BLOB,
    run_id TEXT REFERENCES extraction_runs(id),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    PRIMARY KEY (item_id, rev, source_id),
    FOREIGN KEY (item_id, rev) REFERENCES item_revisions(item_id, rev)
) STRICT;

CREATE TABLE extraction_chunks (
    chunk_hash BLOB NOT NULL,
    model_id TEXT NOT NULL,
    template_version TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES extraction_runs(id),
    status TEXT NOT NULL CHECK (status IN ('done', 'failed')),
    PRIMARY KEY (chunk_hash, model_id, template_version)
) STRICT;

CREATE TABLE conflicts (
    id TEXT NOT NULL PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id),
    rev_a INTEGER NOT NULL,
    rev_b INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'auto_resolved', 'resolved')),
    resolved_by TEXT,
    resolved_at INTEGER
) STRICT;

CREATE TABLE flags (
    item_id TEXT NOT NULL REFERENCES items(id),
    rev INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('dedupe')),
    other_item_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (item_id, rev, type, other_item_id)
) STRICT;

CREATE TABLE pins (
    principal_id TEXT NOT NULL REFERENCES principals(id),
    item_id TEXT NOT NULL REFERENCES items(id),
    PRIMARY KEY (principal_id, item_id)
) STRICT;

CREATE TABLE receipts (
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint BLOB NOT NULL,
    scheme INTEGER NOT NULL CHECK (scheme IN (1, 2)),
    item_id TEXT,
    rev INTEGER,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (principal_id, request_id)
) STRICT;

CREATE TABLE audit (
    id INTEGER PRIMARY KEY,
    at INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    item_id TEXT,
    rev INTEGER,
    project_id TEXT,
    legacy INTEGER NOT NULL DEFAULT 0,
    prev_hash BLOB NOT NULL,
    hash BLOB NOT NULL UNIQUE
) STRICT;

CREATE TABLE erasures (
    item_id TEXT NOT NULL PRIMARY KEY,
    rev INTEGER NOT NULL,
    at INTEGER NOT NULL
) STRICT;

CREATE TABLE artifacts (
    path TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('backup', 'export', 'migration')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
) STRICT;

CREATE TABLE meta (
    name TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE v1_map (
    v1_ref TEXT NOT NULL PRIMARY KEY,
    item_id TEXT NOT NULL
) STRICT;

CREATE TABLE embeddings (
    item_id TEXT NOT NULL,
    rev INTEGER NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (item_id, rev, model)
) STRICT;

CREATE VIRTUAL TABLE fts_items USING fts5(title, body, tokenize = 'unicode61 remove_diacritics 2');

CREATE UNIQUE INDEX items_private_key ON items (owner_id, kind, key)
    WHERE scope_kind = 'private' AND key IS NOT NULL;
CREATE UNIQUE INDEX items_project_key ON items (project_id, kind, key)
    WHERE scope_kind = 'project' AND key IS NOT NULL;
CREATE INDEX item_revisions_by_state ON item_revisions (item_id, state, accepted_at);
CREATE INDEX fact_versions_by_key ON fact_versions (subject, predicate);
CREATE INDEX items_by_scope ON items (scope_kind, owner_id, project_id, kind);
CREATE INDEX provenance_by_source ON provenance (source_id);
CREATE INDEX audit_by_at ON audit (at);

-- Revisions are immutable: only candidate -> accepted|rejected (with review fields), and deletion
-- or retention may null content columns.
CREATE TRIGGER item_revisions_immutable BEFORE UPDATE ON item_revisions
WHEN NEW.item_id IS NOT OLD.item_id OR NEW.rev IS NOT OLD.rev
  OR NEW.valid_from IS NOT OLD.valid_from OR NEW.valid_to IS NOT OLD.valid_to
  OR NEW.actor_id IS NOT OLD.actor_id OR NEW.request_id IS NOT OLD.request_id
  OR NEW.recorded_at IS NOT OLD.recorded_at
  OR (NEW.title IS NOT OLD.title AND NEW.title IS NOT NULL)
  OR (NEW.body IS NOT OLD.body AND NEW.body IS NOT NULL)
  OR (NEW.body_hash IS NOT OLD.body_hash AND NEW.body_hash IS NOT NULL)
  OR (NEW.meta_json IS NOT OLD.meta_json AND NEW.meta_json IS NOT NULL)
  OR (NEW.state IS NOT OLD.state AND NOT (OLD.state = 'candidate' AND NEW.state IN ('accepted', 'rejected')))
  OR (OLD.state <> 'candidate' AND (NEW.accepted_at IS NOT OLD.accepted_at
      OR NEW.reviewed_by IS NOT OLD.reviewed_by OR NEW.reviewed_at IS NOT OLD.reviewed_at))
BEGIN
  SELECT RAISE(ABORT, 'item_revisions rows are immutable');
END;

CREATE TRIGGER fact_versions_immutable BEFORE UPDATE ON fact_versions
WHEN NEW.item_id IS NOT OLD.item_id OR NEW.rev IS NOT OLD.rev OR NEW.op IS NOT OLD.op
  OR NEW.supersedes_rev IS NOT OLD.supersedes_rev
  OR (NEW.subject IS NOT OLD.subject AND NEW.subject IS NOT NULL)
  OR (NEW.predicate IS NOT OLD.predicate AND NEW.predicate IS NOT NULL)
  OR (NEW.value_json IS NOT OLD.value_json AND NEW.value_json IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'fact_versions rows are immutable');
END;

CREATE TRIGGER audit_append_only BEFORE UPDATE ON audit
BEGIN
  SELECT RAISE(ABORT, 'audit is append-only');
END;
"""

SCHEMA_V11 = """
-- Per-document, per-column term instances of fts_items (term, doc, col, offset). Read with a term
-- constraint to get term frequencies; document lengths come from fts_items_docsize.
CREATE VIRTUAL TABLE fts_items_vocab USING fts5vocab(fts_items, instance);

-- Deleting an FTS row removes its index entries at once instead of leaving delete markers.
INSERT INTO fts_items (fts_items, rank) VALUES ('secure-delete', 1);

CREATE INDEX items_by_kind_key ON items (kind, key) WHERE key IS NOT NULL;
"""

# version -> DDL that brings a store from the previous version to this one.
MIGRATIONS = {10: SCHEMA_V10, 11: SCHEMA_V11}
FIRST_VERSION = 10
