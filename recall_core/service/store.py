"""The isolated service store. Principal operations authorize inside their transaction."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile
import time

from ..context_policy import validate_context


APPLICATION_ID = 0x52434F42  # RCOB
MIGRATIONS = (
    """
    CREATE TABLE bots (
        id TEXT PRIMARY KEY,
        credential BLOB UNIQUE
    );
    CREATE TABLE checkpoints (
        bot TEXT NOT NULL REFERENCES bots(id),
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        revision INTEGER NOT NULL,
        updated TEXT NOT NULL,
        PRIMARY KEY (bot, scope, key)
    );
    CREATE TABLE checkpoint_revisions (
        bot TEXT NOT NULL,
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        revision INTEGER NOT NULL,
        updated TEXT NOT NULL,
        content TEXT NOT NULL,
        PRIMARY KEY (bot, scope, key, revision),
        FOREIGN KEY (bot, scope, key) REFERENCES checkpoints(bot, scope, key)
    );
    CREATE TABLE receipts (
        bot TEXT NOT NULL REFERENCES bots(id),
        request_id TEXT NOT NULL,
        fingerprint BLOB NOT NULL,
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        revision INTEGER NOT NULL,
        PRIMARY KEY (bot, request_id),
        FOREIGN KEY (bot, scope, key, revision)
            REFERENCES checkpoint_revisions(bot, scope, key, revision) ON DELETE CASCADE
    );
    CREATE INDEX receipts_by_revision ON receipts (bot, scope, key, revision);
    CREATE INDEX checkpoints_by_updated ON checkpoint_revisions (updated);
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        subject TEXT,
        project TEXT,
        revision INTEGER
    );
    """,
    """
    CREATE TABLE projects (id TEXT PRIMARY KEY);
    CREATE TABLE memberships (
        bot TEXT NOT NULL REFERENCES bots(id),
        project TEXT NOT NULL REFERENCES projects(id),
        PRIMARY KEY (bot, project)
    );
    CREATE TABLE proposals (
        id TEXT PRIMARY KEY,
        project TEXT NOT NULL REFERENCES projects(id),
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        author TEXT NOT NULL REFERENCES bots(id),
        expected_revision INTEGER NOT NULL,
        request_id TEXT NOT NULL,
        fingerprint BLOB NOT NULL,
        content TEXT NOT NULL,
        submitted TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        reviewer TEXT,
        reviewed TEXT,
        revision INTEGER,
        UNIQUE (author, kind, request_id),
        UNIQUE (project, kind, key, revision)
    );
    CREATE INDEX proposals_by_project ON proposals (project, status, submitted);
    CREATE INDEX proposals_by_review ON proposals (status, reviewed);
    CREATE TABLE publications (
        project TEXT NOT NULL REFERENCES projects(id),
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        revision INTEGER NOT NULL,
        proposal_id TEXT REFERENCES proposals(id),
        PRIMARY KEY (project, kind, key)
    );
    """,
    """
    ALTER TABLE bots RENAME TO principals;
    ALTER TABLE checkpoints RENAME COLUMN bot TO principal;
    ALTER TABLE checkpoint_revisions RENAME COLUMN bot TO principal;
    ALTER TABLE receipts RENAME COLUMN bot TO principal;
    ALTER TABLE memberships RENAME COLUMN bot TO principal;
    """,
)


class StoreError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.result = {"ok": False, "error": code, "message": message, **details}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def retention_cutoff(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="microseconds")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def credential_digest(token: str):
    return sha256(token.encode("ascii")).digest()


def content_contains(payload, query):
    return any(query in value.casefold()
               for field in json.loads(payload).values()
               for value in (field if isinstance(field, list) else [field]))


@contextmanager
def new_private_file(path):
    """Stage a 0600 file next to its destination and publish it without overwriting."""
    if os.path.lexists(path):
        raise StoreError("conflict", "The destination already exists.")
    with tempfile.NamedTemporaryFile(prefix=".recall-service-", dir=path.parent, delete=False) as file:
        staged = Path(file.name)
    try:
        yield staged
        with staged.open("rb") as file:
            os.fsync(file.fileno())
        try:
            os.link(staged, path)
        except FileExistsError:
            raise StoreError("conflict", "The destination already exists.") from None
    finally:
        staged.unlink()


class ServiceStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().absolute()

    def _check_private(self, path, *, directory=False):
        info = path.lstat()
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not expected_type(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)
        ):
            raise StoreError("unsafe_path", "Use an owned 0700 directory and 0600 regular files.")

    def _connect(self):
        self._check_private(self.path.parent, directory=True)
        self._check_private(self.path)
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA secure_delete = ON")
            db.execute("PRAGMA temp_store = MEMORY")
        except sqlite3.Error:
            db.close()
            raise
        return db

    @contextmanager
    def transaction(self, *, write=False, check_schema=True):
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if check_schema:
                    self._check_schema(db, current=True)
                yield db
        finally:
            db.close()

    def _check_schema(self, db, *, current):
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if app_id == 0 and not current:
            if version == 0 and db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is None:
                return 0
        if app_id != APPLICATION_ID or not 0 < version <= len(MIGRATIONS):
            raise StoreError("wrong_store", "Use a supported Recall service database.")
        if current and version != len(MIGRATIONS):
            raise StoreError("schema_version", "Run recall service init to migrate the database.")
        return version

    def initialize(self, *, create=False):
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_private(self.path.parent, directory=True)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self._backup_before_migration()
        with self.transaction(write=True, check_schema=False) as db:
            current = self._check_schema(db, current=False)
            for version, migration in enumerate(MIGRATIONS[current:], start=current + 1):
                # sqlite3 executescript commits implicitly. Keep DDL and the version atomic.
                for statement in migration.strip().split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                db.execute(f"PRAGMA user_version = {version}")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StoreError("migration_failed", "Foreign-key check failed; the migration was rolled back.")
        return {"schema_version": len(MIGRATIONS)}

    def _backup_before_migration(self):
        if not self.path.exists():
            return
        source = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            app_id = source.execute("PRAGMA application_id").fetchone()[0]
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if app_id != APPLICATION_ID or not 0 < version < len(MIGRATIONS):
                return
            backup = self.path.with_name(f"{self.path.name}.v{version}.bak")
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raise StoreError("backup_exists", "A backup from an earlier migration exists; move it aside first.") from None
            os.close(fd)
            target = sqlite3.connect(backup)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    def health(self):
        with self.transaction() as db:
            db.execute("SELECT id FROM principals LIMIT 1").fetchone()
        return {"status": "ok"}

    def _principal(self, db, digest):
        if digest is not None:
            row = db.execute("SELECT id FROM principals WHERE credential = ?", (digest,)).fetchone()
            if row is not None:
                return row["id"]
        raise StoreError("unauthorized", "A current bot credential is required.")

    def authorize(self, digest):
        with self.transaction() as db:
            self._principal(db, digest)

    def _audit(self, db, actor, action, *, subject=None, project=None, revision=None):
        db.execute(
            "INSERT INTO audit (at, actor, action, subject, project, revision) VALUES (?, ?, ?, ?, ?, ?)",
            (timestamp(), actor, action, subject, project, revision),
        )

    def change_credential(self, bot, action):
        token = None if action == "principal-revoke" else secrets.token_urlsafe(32)
        digest = credential_digest(token) if token else None
        with self.transaction(write=True) as db:
            if action == "principal-add":
                exists = db.execute("SELECT 1 FROM principals WHERE id = ?", (bot,)).fetchone()
                if exists:
                    raise StoreError("conflict", "Bot already exists.")
                db.execute("INSERT INTO principals (id, credential) VALUES (?, ?)", (bot, digest))
            elif not db.execute("UPDATE principals SET credential = ? WHERE id = ?", (digest, bot)).rowcount:
                raise StoreError("not_found", "Bot not found.")
            self._audit(db, "operator", action, subject=bot)
        return {"bot": bot, "token": token} if token else {"bot": bot}

    def _check_revision(self, row, expected_revision):
        current = row["revision"] if row else 0
        if current != expected_revision:
            raise StoreError("conflict", "The revision has changed.", current_revision=current)

    def _checkpoint(self, db, bot, scope, key):
        return db.execute(
            """SELECT c.revision, c.updated, r.content FROM checkpoints AS c
               LEFT JOIN checkpoint_revisions AS r USING (principal, scope, key, revision)
               WHERE c.principal = ? AND c.scope = ? AND c.key = ?""",
            (bot, scope, key),
        ).fetchone()

    def checkpoint_get(self, digest, scope, key, revision=None):
        with self.transaction() as db:
            bot = self._principal(db, digest)
            if revision is None:
                row = self._checkpoint(db, bot, scope, key)
            else:
                row = db.execute(
                    """SELECT revision, updated, content FROM checkpoint_revisions
                       WHERE principal = ? AND scope = ? AND key = ? AND revision = ?""",
                    (bot, scope, key, revision),
                ).fetchone()
                if row is None:
                    raise StoreError("not_found", "No checkpoint revision was found.")
        if row is None:
            return {"revision": 0, "checkpoint": None}
        return {"revision": row["revision"], "updated": row["updated"],
                "checkpoint": json.loads(row["content"]) if row["content"] is not None else None}

    def _checkpoint_retry(self, db, bot, request_id, payload):
        fingerprint = sha256(canonical_json(payload).encode("utf-8")).digest()
        receipt = db.execute(
            "SELECT fingerprint, revision FROM receipts WHERE principal = ? AND request_id = ?",
            (bot, request_id),
        ).fetchone()
        if receipt and receipt["fingerprint"] != fingerprint:
            raise StoreError("request_id_conflict", "The request ID is bound to a different write.")
        return receipt, fingerprint

    def checkpoint_save(self, digest, scope, key, expected_revision, request_id, checkpoint):
        with self.transaction(write=True) as db:
            bot = self._principal(db, digest)
            receipt, fingerprint = self._checkpoint_retry(
                db, bot, request_id, [scope, key, expected_revision, checkpoint],
            )
            if receipt:
                return {"revision": receipt["revision"]}
            self._check_revision(self._checkpoint(db, bot, scope, key), expected_revision)
            revision = expected_revision + 1
            now = timestamp()
            db.execute(
                """INSERT INTO checkpoints (principal, scope, key, revision, updated)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (principal, scope, key) DO UPDATE SET
                   revision = excluded.revision, updated = excluded.updated""",
                (bot, scope, key, revision, now),
            )
            db.execute(
                "INSERT INTO checkpoint_revisions VALUES (?, ?, ?, ?, ?, ?)",
                (bot, scope, key, revision, now, canonical_json(checkpoint)),
            )
            db.execute(
                "INSERT INTO receipts (principal, request_id, fingerprint, scope, key, revision) VALUES (?, ?, ?, ?, ?, ?)",
                (bot, request_id, fingerprint, scope, key, revision),
            )
            self._audit(db, f"bot:{bot}", "checkpoint-save", revision=revision)
        return {"revision": revision}

    def project_add(self, project):
        with self.transaction(write=True) as db:
            if not db.execute("INSERT OR IGNORE INTO projects VALUES (?)", (project,)).rowcount:
                raise StoreError("conflict", "Project already exists.")
            self._audit(db, "operator", "project-add", project=project)
        return {"project": project}

    def change_membership(self, project, bot, *, grant):
        with self.transaction(write=True) as db:
            if (db.execute("SELECT 1 FROM projects WHERE id = ?", (project,)).fetchone() is None
                    or db.execute("SELECT 1 FROM principals WHERE id = ?", (bot,)).fetchone() is None):
                raise StoreError("not_found", "Project or bot not found.")
            if grant:
                changed = db.execute("INSERT OR IGNORE INTO memberships VALUES (?, ?)", (bot, project)).rowcount
            else:
                changed = db.execute("DELETE FROM memberships WHERE principal = ? AND project = ?", (bot, project)).rowcount
                db.execute("DELETE FROM proposals WHERE author = ? AND project = ? AND status = 'pending'",
                           (bot, project))
            if changed:
                self._audit(db, "operator", "member-grant" if grant else "member-revoke",
                            subject=bot, project=project)
        return {"project": project, "bot": bot, "member": grant}

    def _member(self, db, bot, project):
        if db.execute("SELECT 1 FROM memberships WHERE principal = ? AND project = ?", (bot, project)).fetchone() is None:
            raise StoreError("not_found", "Project not found for this bot.")

    def _publication(self, db, project, kind, key):
        return db.execute(
            """SELECT current.revision, p.expected_revision, p.author, p.submitted, p.reviewer, p.reviewed, p.content
               FROM publications AS current
               LEFT JOIN proposals AS p ON p.id = current.proposal_id
               WHERE current.project = ? AND current.kind = ? AND current.key = ?""",
            (project, kind, key),
        ).fetchone()

    def _publication_data(self, row):
        if row is None:
            raise StoreError("not_found", "No accepted revision was found.")
        if row["content"] is None:
            raise StoreError("not_found", "No accepted revision was found.", current_revision=row["revision"])
        return {
            "revision": row["revision"],
            "previous_revision": row["expected_revision"],
            "author": row["author"],
            "submitted": row["submitted"],
            "reviewer": row["reviewer"],
            "reviewed": row["reviewed"],
            **json.loads(row["content"]),
        }

    def publication_get(self, digest, kind, project, key):
        with self.transaction() as db:
            bot = self._principal(db, digest)
            self._member(db, bot, project)
            return self._publication_data(self._publication(db, project, kind, key))

    def publication_propose(self, digest, kind, project, key, expected_revision, request_id, content):
        serialized = canonical_json(content)
        fingerprint = sha256(canonical_json([project, key, expected_revision, content]).encode("utf-8")).digest()
        with self.transaction(write=True) as db:
            bot = self._principal(db, digest)
            self._member(db, bot, project)
            previous = db.execute(
                "SELECT id, fingerprint FROM proposals WHERE author = ? AND kind = ? AND request_id = ?",
                (bot, kind, request_id),
            ).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise StoreError("request_id_conflict", "The request ID is bound to a different write.")
                return {"proposal_id": previous["id"], "status": "pending"}
            current = self._publication(db, project, kind, key)
            self._check_revision(current, expected_revision)
            if kind == "brief" and current is not None and current["content"] is not None:
                try:
                    validate_context(content["markdown"], original=json.loads(current["content"])["markdown"],
                                     require_structured=True)
                except ValueError:
                    raise StoreError("invalid_input", "A revision must preserve the Markdown format version.") from None
            proposal_id = secrets.token_hex(16)
            db.execute(
                """INSERT INTO proposals (id, project, kind, key, author, expected_revision, request_id,
                                          fingerprint, content, submitted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (proposal_id, project, kind, key, bot, expected_revision, request_id, fingerprint,
                 serialized, timestamp()),
            )
            self._audit(db, f"bot:{bot}", f"{kind}-propose", project=project)
        return {"proposal_id": proposal_id, "status": "pending"}

    def list_proposals(self, project, status, limit):
        with self.transaction() as db:
            rows = db.execute(
                """SELECT id AS proposal_id, project, kind, key, author, expected_revision, submitted,
                          status, reviewer, reviewed, revision
                   FROM proposals WHERE project = ? AND status = ? ORDER BY submitted DESC, id LIMIT ?""",
                (project, status, limit),
            )
            return {"proposals": [dict(row) for row in rows]}

    def _proposal(self, db, proposal_id):
        row = db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise StoreError("not_found", "Proposal not found.")
        return row

    def show_proposal(self, proposal_id):
        with self.transaction() as db:
            row = dict(self._proposal(db, proposal_id))
        content = json.loads(row.pop("content"))
        del row["fingerprint"], row["request_id"]
        row["proposal_id"] = row.pop("id")
        return {**row, **content}

    def review(self, proposal_id, decision, reviewer):
        status = "accepted" if decision == "approve" else "rejected"
        with self.transaction(write=True) as db:
            row = self._proposal(db, proposal_id)
            project, kind, key = row["project"], row["kind"], row["key"]
            if row["status"] != "pending":
                if row["status"] == status and row["reviewer"] == reviewer:
                    return {"proposal_id": proposal_id, "status": status, "revision": row["revision"]}
                raise StoreError("conflict", "The proposal has already been reviewed.")
            revision = None
            if status == "accepted":
                self._member(db, row["author"], project)
                self._check_revision(self._publication(db, project, kind, key), row["expected_revision"])
                revision = row["expected_revision"] + 1
                db.execute(
                    """INSERT INTO publications (project, kind, key, revision, proposal_id) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (project, kind, key) DO UPDATE SET
                       proposal_id = excluded.proposal_id, revision = excluded.revision""",
                    (project, kind, key, revision, proposal_id),
                )
            db.execute(
                "UPDATE proposals SET status = ?, reviewer = ?, reviewed = ?, revision = ? WHERE id = ?",
                (status, reviewer, timestamp(), revision, proposal_id),
            )
            self._audit(db, f"operator:{reviewer}", f"{kind}-{decision}", project=project, revision=revision)
        return {"proposal_id": proposal_id, "status": status, "revision": revision}

    def export_brief(self, project, key):
        with self.transaction() as db:
            return self._publication_data(self._publication(db, project, "brief", key))["markdown"]

    def project_search(self, digest, query, project=None, limit=10):
        with self.transaction() as db:
            bot = self._principal(db, digest)
            db.create_function("fold", 1, str.casefold, deterministic=True)
            db.create_function("content_contains", 2, content_contains, deterministic=True)
            rows = db.execute(
                """SELECT 'checkpoint' AS kind, c.scope, c.key,
                          CASE WHEN c.scope = 'project' THEN c.key END AS project, c.revision, c.updated AS changed
                   FROM checkpoints AS c JOIN checkpoint_revisions AS r USING (principal, scope, key, revision)
                   WHERE c.principal = :principal
                     AND (:project IS NULL OR (c.scope = 'project' AND c.key = :project))
                     AND (instr(fold(c.key), :query) > 0 OR content_contains(r.content, :query))
                   UNION ALL
                   SELECT current.kind, 'project', current.key, current.project, current.revision, p.reviewed
                   FROM memberships AS m
                   JOIN publications AS current ON m.project = current.project
                   JOIN proposals AS p ON p.id = current.proposal_id
                   WHERE m.principal = :principal AND (:project IS NULL OR m.project = :project)
                     AND (instr(fold(current.key), :query) > 0 OR content_contains(p.content, :query))
                   ORDER BY changed DESC, kind, project, key LIMIT :limit""",
                {"principal": bot, "project": project, "query": query.strip().casefold(), "limit": limit},
            )
            return {"matches": [dict(row) for row in rows]}

    def _erase_checkpoint(self, db, bot, scope, key, previous_revision, now):
        revision = previous_revision + 1
        db.execute(
            "UPDATE checkpoints SET revision = ?, updated = ? WHERE principal = ? AND scope = ? AND key = ?",
            (revision, now, bot, scope, key),
        )
        count = db.execute("DELETE FROM checkpoint_revisions WHERE principal = ? AND scope = ? AND key = ?",
                           (bot, scope, key)).rowcount
        return revision, count

    def delete_checkpoint(self, bot, scope, key):
        with self.transaction(write=True) as db:
            row = self._checkpoint(db, bot, scope, key)
            if row is None:
                raise StoreError("not_found", "Checkpoint not found.")
            revision = row["revision"]
            if row["content"] is not None:
                revision, _ = self._erase_checkpoint(db, bot, scope, key, revision, timestamp())
                self._audit(db, "operator", "checkpoint-delete", subject=bot, revision=revision)
        return {"revision": revision}

    def _remove_current_publication(self, db, project, kind, key, previous_revision):
        revision = previous_revision + 1
        db.execute(
            """INSERT INTO publications (project, kind, key, revision, proposal_id) VALUES (?, ?, ?, ?, NULL)
               ON CONFLICT (project, kind, key) DO UPDATE SET revision = excluded.revision, proposal_id = NULL""",
            (project, kind, key, revision),
        )
        return revision

    def delete_publication(self, project, kind, key):
        with self.transaction(write=True) as db:
            current = self._publication(db, project, kind, key)
            pending_or_history = db.execute(
                "SELECT 1 FROM proposals WHERE project = ? AND kind = ? AND key = ? LIMIT 1",
                (project, kind, key),
            ).fetchone()
            if pending_or_history is None:
                if current is None:
                    raise StoreError("not_found", "No publication or proposal was found.")
                return {"revision": current["revision"]}
            revision = self._remove_current_publication(db, project, kind, key, current["revision"] if current else 0)
            db.execute("DELETE FROM proposals WHERE project = ? AND kind = ? AND key = ?", (project, kind, key))
            self._audit(db, "operator", "publication-delete", project=project, revision=revision)
        return {"revision": revision}

    def prune(self, checkpoint_days, publication_days, proposal_days, audit_days):
        with self.transaction(write=True) as db:
            checkpoint_cutoff = retention_cutoff(checkpoint_days)
            checkpoints = db.execute(
                """SELECT c.principal, c.scope, c.key, c.revision FROM checkpoints AS c
                   JOIN checkpoint_revisions AS r USING (principal, scope, key, revision) WHERE r.updated < ?""",
                (checkpoint_cutoff,),
            ).fetchall()
            now = timestamp()
            checkpoint_count = sum(self._erase_checkpoint(db, *row, now)[1] for row in checkpoints)
            checkpoint_count += db.execute("DELETE FROM checkpoint_revisions WHERE updated < ?",
                                           (checkpoint_cutoff,)).rowcount

            publication_cutoff = retention_cutoff(publication_days)
            publications = db.execute(
                """SELECT current.project, current.kind, current.key, current.revision FROM publications AS current
                   JOIN proposals AS p ON p.id = current.proposal_id WHERE p.reviewed < ?""",
                (publication_cutoff,),
            ).fetchall()
            for row in publications:
                self._remove_current_publication(db, *row)
            proposal_cutoff = retention_cutoff(proposal_days)
            proposal_count = db.execute(
                """DELETE FROM proposals WHERE
                     (status = 'accepted' AND reviewed < ?
                      AND id NOT IN (SELECT proposal_id FROM publications WHERE proposal_id IS NOT NULL))
                     OR (status = 'pending' AND submitted < ?)
                     OR (status = 'rejected' AND reviewed < ?)""",
                (publication_cutoff, proposal_cutoff, proposal_cutoff),
            ).rowcount
            audit_count = db.execute("DELETE FROM audit WHERE at < ?", (retention_cutoff(audit_days),)).rowcount
            self._audit(db, "operator", "prune")
        return {"checkpoint_revisions": checkpoint_count, "proposals": proposal_count, "audit_events": audit_count}

    def audit_events(self, limit):
        with self.transaction() as db:
            rows = db.execute(
                "SELECT at, actor, action, subject, project, revision FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return {"events": [dict(row) for row in rows]}

    def inventory(self, limit):
        with self.transaction() as db:
            bots = db.execute("SELECT id, credential IS NOT NULL AS active FROM principals ORDER BY id LIMIT ?", (limit,))
            projects = db.execute("SELECT id FROM projects ORDER BY id LIMIT ?", (limit,))
            return {"bots": [{"id": row["id"], "active": bool(row["active"])} for row in bots],
                    "projects": [row["id"] for row in projects]}

    def member_list(self, project, limit):
        with self.transaction() as db:
            members = db.execute("SELECT principal FROM memberships WHERE project = ? ORDER BY principal LIMIT ?",
                                 (project, limit))
            return {"project": project, "members": [row["principal"] for row in members]}

    def copy_to(self, target, *, action):
        """Write a consistent, credential-free snapshot to a new private path."""
        target.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target._check_private(target.path.parent, directory=True)
        with new_private_file(target.path) as working_path:
            working = ServiceStore(working_path)
            source = self._connect()
            try:
                if not self._check_schema(source, current=False):
                    raise StoreError("wrong_store", "Use a supported Recall service database.")
                dest = working._connect()
                try:
                    deadline = time.monotonic() + 15

                    def check_deadline(_status, _remaining, _total):
                        if time.monotonic() > deadline:
                            raise StoreError("storage_unavailable", "The snapshot timed out.")

                    source.backup(dest, pages=256, progress=check_deadline, sleep=0.05)
                finally:
                    dest.close()
            finally:
                source.close()

            working.initialize()
            with working.transaction(write=True) as db:
                if (db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                        or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
                    raise StoreError("wrong_store", "The snapshot failed its integrity check.")
                db.execute("UPDATE principals SET credential = NULL")
                working._audit(db, "operator", action)
        return {"status": "ok"}
