"""Import a v1 store (user_version 2 or 3) into a NEW v2 store file.

The v1 file is opened read-only and never modified. Rows are mapped as follows:

- bots/principals(id, credential) -> principals; token_digest = credential; created_at = mt;
  revoked_at = mt if the credential is NULL.
- projects, memberships -> same rows; created_at / added_at = mt.
- checkpoints -> private checkpoint item, key = "scope:key", current_rev = revision; a checkpoint whose
  current revision row is missing becomes a tombstone (deleted_at = updated, key NULL).
- checkpoint_revisions -> accepted revisions, body = content, recorded_at = accepted_at = valid_from = updated.
- proposals -> revisions of one project item per (project, kind, key), owner = first author;
  pending/accepted/rejected -> candidate/accepted/rejected; rev = revision, else the next free rev in
  submitted order; recorded_at = valid_from = submitted; reviewed_by = reviewer; reviewed_at = reviewed,
  and accepted_at = reviewed for accepted proposals.
- publications -> current_rev = revision; proposal_id NULL -> tombstone (deleted_at = mt, key NULL).
- receipts -> scheme 1, recorded_at = mt.
- audit -> legacy = 1, hashed into the chain in id order.

Every v1 reference is recorded in v1_map. The run fails with migration_failed unless every v1 row maps to
exactly one v2 row, foreign_key_check is empty and integrity_check is ok. A JSON mapping report with
per-table counts is written beside the new file.
"""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Callable, Optional

from ..core import normalise, timefmt
from ..core.errors import RecallError
from ..core.model import RECEIPT_TTL_MICROS
from ..store import audit
from ..store.connection import check_engine, check_private, write_transaction
from ..store.repository import Store, system_clock
from ..store.schema import APPLICATION_ID, MIGRATIONS

SUPPORTED_V1_VERSIONS = (2, 3)
REPORT_SUFFIX = ".migration-report.json"
STATE_MAP = {"pending": "candidate", "accepted": "accepted", "rejected": "rejected"}


def report_path(dest: Path) -> Path:
    return dest.with_name(dest.name + REPORT_SUFFIX)


def file_sha256(path: Path) -> str:
    digest = sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def v1_ref(*parts) -> str:
    return normalise.canonical_json(list(parts))


def read_v1(source: Path) -> tuple[int, dict[str, list[dict]]]:
    """Read every v1 table through a read-only connection, inside one read transaction."""
    try:
        db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    except sqlite3.Error:
        raise RecallError("wrong_store", "The v1 store cannot be opened.") from None
    db.row_factory = sqlite3.Row
    try:
        try:
            db.execute("BEGIN")
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError:
            raise RecallError("wrong_store", "Not a Recall store.") from None
        if app_id != APPLICATION_ID:
            raise RecallError("wrong_store", "Not a Recall store.")
        if version in MIGRATIONS:
            raise RecallError("wrong_store", "The source is already a v2 store.")
        if version > max(SUPPORTED_V1_VERSIONS):
            raise RecallError("schema_version", "The source store is newer than this build.", user_version=version)
        if version not in SUPPORTED_V1_VERSIONS:
            raise RecallError("wrong_store", "Unsupported v1 store version.", user_version=version)
        who = "bot" if version == 2 else "principal"
        principals = "bots" if version == 2 else "principals"

        def rows(sql: str) -> list[dict]:
            return [dict(row) for row in db.execute(sql)]

        data = {
            "principals": rows(f"SELECT id, credential FROM {principals} ORDER BY id"),
            "projects": rows("SELECT id FROM projects ORDER BY id"),
            "memberships": rows(f"SELECT {who} AS principal, project FROM memberships ORDER BY {who}, project"),
            "checkpoints": rows(f"SELECT {who} AS principal, scope, key, revision, updated FROM checkpoints"
                                f" ORDER BY {who}, scope, key"),
            "checkpoint_revisions": rows(
                f"SELECT {who} AS principal, scope, key, revision, updated, content FROM checkpoint_revisions"
                f" ORDER BY {who}, scope, key, revision"),
            "receipts": rows(f"SELECT {who} AS principal, request_id, fingerprint, scope, key, revision FROM receipts"
                             f" ORDER BY {who}, request_id"),
            "audit": rows("SELECT id, at, actor, action, subject, project, revision FROM audit ORDER BY id"),
            "proposals": rows("SELECT * FROM proposals ORDER BY project, kind, key, submitted, id"),
            "publications": rows("SELECT project, kind, key, revision, proposal_id FROM publications"
                                 " ORDER BY project, kind, key"),
        }
        db.execute("ROLLBACK")
        return version, data
    except sqlite3.Error as exc:
        raise RecallError("wrong_store", "The v1 store could not be read.", reason=type(exc).__name__) from None
    finally:
        db.close()


def _proposal_body(kind: str, content: str) -> tuple[Optional[str], dict]:
    """Unwrap v1 proposal content: brief {"markdown"} and excerpt {"excerpt", "source_ref"}."""
    meta: dict = {}
    try:
        parsed = json.loads(content)
    except ValueError:
        parsed = None
    if kind == "brief" and isinstance(parsed, dict) and set(parsed) == {"markdown"} and isinstance(parsed["markdown"], str):
        return parsed["markdown"], meta
    if (kind == "excerpt" and isinstance(parsed, dict) and set(parsed) == {"excerpt", "source_ref"}
            and all(isinstance(value, str) for value in parsed.values())):
        meta["source_ref"] = parsed["source_ref"]
        return parsed["excerpt"], meta
    meta["v1_content"] = "json"
    return content, meta


class _Builder:
    def __init__(self, store: Store, mt: int):
        self.store = store
        self.db = store.db
        self.mt = mt
        self.notes: list[str] = []
        self.unmapped: list[dict] = []

    def ulid(self) -> str:
        return self.store._ulid(self.mt)

    def insert_item(self, *, kind, scope, owner, project, key, current_rev, created_at, deleted_at) -> str:
        item_id = self.ulid()
        self.db.execute(
            "INSERT INTO items (id, kind, scope_kind, owner_id, project_id, key, current_rev, created_at, deleted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, kind, scope, owner, project, None if deleted_at is not None else key, current_rev,
             created_at, deleted_at))
        if deleted_at is not None:
            self.db.execute("INSERT INTO erasures (item_id, rev, at) VALUES (?, ?, ?)", (item_id, current_rev, deleted_at))
        return item_id

    def insert_revision(self, item_id, rev, *, state, body, meta, valid_from, actor, request_id, recorded_at,
                        accepted_at, reviewed_by=None, reviewed_at=None) -> None:
        self.db.execute(
            "INSERT INTO item_revisions (item_id, rev, state, body, body_hash, meta_json, valid_from, actor_id,"
            " request_id, recorded_at, accepted_at, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, rev, state, body, None if body is None else normalise.body_hash(body),
             None if meta is None else normalise.canonical_json(meta), valid_from, actor, request_id, recorded_at,
             accepted_at, reviewed_by, reviewed_at))

    def index_current(self, item_id: str, kind: str, current_rev: int) -> None:
        exists = self.db.execute("SELECT 1 FROM item_revisions WHERE item_id = ? AND rev = ? AND state = 'accepted'"
                                 " AND body IS NOT NULL", (item_id, current_rev)).fetchone()
        if exists:
            self.store._fts_index(self.db, item_id, current_rev, kind)

    def map_ref(self, ref: str, item_id: str) -> None:
        self.db.execute("INSERT INTO v1_map (v1_ref, item_id) VALUES (?, ?)", (ref, item_id))

    def build(self, data: dict[str, list[dict]]) -> None:
        mt, db = self.mt, self.db
        for row in data["principals"]:
            db.execute("INSERT INTO principals (id, token_digest, created_at, revoked_at) VALUES (?, ?, ?, ?)",
                       (row["id"], row["credential"], mt, mt if row["credential"] is None else None))
        for row in data["projects"]:
            db.execute("INSERT INTO projects (id, created_at) VALUES (?, ?)", (row["id"], mt))
        for row in data["memberships"]:
            db.execute("INSERT INTO memberships (principal_id, project_id, added_at) VALUES (?, ?, ?)",
                       (row["principal"], row["project"], mt))
        self.build_checkpoints(data)
        self.build_publications(data)
        for row in data["audit"]:
            audit.append(db, at=timefmt.parse_rfc3339(row["at"]), actor=row["actor"], action=row["action"],
                         item_id=row["subject"], rev=row["revision"], project_id=row["project"], legacy=1)

    def build_checkpoints(self, data) -> None:
        revisions = defaultdict(list)
        for row in data["checkpoint_revisions"]:
            revisions[(row["principal"], row["scope"], row["key"])].append(row)
        self.checkpoint_items: dict[tuple, str] = {}
        for row in data["checkpoints"]:
            ident = (row["principal"], row["scope"], row["key"])
            revs = revisions.pop(ident, [])
            has_current = any(r["revision"] == row["revision"] for r in revs)
            updated = timefmt.parse_rfc3339(row["updated"])
            created = min([timefmt.parse_rfc3339(r["updated"]) for r in revs] + [updated])
            item_id = self.insert_item(kind="checkpoint", scope="private", owner=row["principal"], project=None,
                                       key=f"{row['scope']}:{row['key']}", current_rev=row["revision"],
                                       created_at=created, deleted_at=None if has_current else updated)
            self.checkpoint_items[ident] = item_id
            self.map_ref(v1_ref("checkpoints", *ident), item_id)
            for rev in revs:
                at = timefmt.parse_rfc3339(rev["updated"])
                self.insert_revision(item_id, rev["revision"], state="accepted",
                                     body=rev["content"] if has_current else None, meta=None, valid_from=at,
                                     actor=row["principal"], request_id=None, recorded_at=at, accepted_at=at)
                self.map_ref(v1_ref("checkpoint_revisions", *ident, rev["revision"]), item_id)
            if has_current:
                self.index_current(item_id, "checkpoint", row["revision"])
            else:
                self.notes.append(f"checkpoint without a current revision imported as a tombstone: {item_id}")
        for ident, revs in revisions.items():
            for rev in revs:
                self.unmapped.append({"table": "checkpoint_revisions", "ref": v1_ref(*ident, rev["revision"]),
                                      "reason": "no parent checkpoint"})
        for row in data["receipts"]:
            item_id = self.checkpoint_items.get((row["principal"], row["scope"], row["key"]))
            if item_id is None:
                self.unmapped.append({"table": "receipts", "ref": v1_ref(row["principal"], row["request_id"]),
                                      "reason": "no parent checkpoint"})
                continue
            self.db.execute(
                "INSERT INTO receipts (principal_id, request_id, fingerprint, scheme, item_id, rev, recorded_at)"
                " VALUES (?, ?, ?, 1, ?, ?, ?)",
                (row["principal"], row["request_id"], row["fingerprint"], item_id, row["revision"], self.mt))

    def _fallback_owner(self, project: str, data) -> Optional[str]:
        members = sorted(row["principal"] for row in data["memberships"] if row["project"] == project)
        principals = sorted(row["id"] for row in data["principals"])
        return (members or principals or [None])[0]

    def _project_item(self, group, proposals, *, current_rev, deleted, owner) -> str:
        project, kind, key = group
        created = min([timefmt.parse_rfc3339(p["submitted"]) for p in proposals] or [self.mt])
        item_id = self.insert_item(kind=kind, scope="project", owner=owner, project=project, key=key,
                                   current_rev=current_rev, created_at=created,
                                   deleted_at=self.mt if deleted else None)
        taken = {p["revision"] for p in proposals if p["status"] == "accepted" and p["revision"] is not None}
        counter = max(taken | {current_rev})
        for proposal in sorted(proposals, key=lambda p: (p["submitted"], p["id"])):
            if proposal["status"] == "accepted" and proposal["revision"] is not None:
                rev = proposal["revision"]
            else:
                counter += 1
                rev = counter
            state = STATE_MAP.get(proposal["status"])
            if state is None:
                self.unmapped.append({"table": "proposals", "ref": v1_ref("proposals", proposal["id"]),
                                      "reason": f"unknown status {proposal['status']!r}"})
                continue
            body, meta = _proposal_body(kind, proposal["content"])
            meta = {"base_rev": proposal["expected_revision"], **meta}
            submitted = timefmt.parse_rfc3339(proposal["submitted"])
            reviewed = None if proposal["reviewed"] is None else timefmt.parse_rfc3339(proposal["reviewed"])
            self.insert_revision(
                item_id, rev, state=state, body=None if deleted else body, meta=None if deleted else meta,
                valid_from=submitted, actor=proposal["author"], request_id=proposal["request_id"],
                recorded_at=submitted, accepted_at=reviewed if state == "accepted" else None,
                reviewed_by=proposal["reviewer"], reviewed_at=reviewed)
            self.map_ref(v1_ref("proposals", proposal["id"]), item_id)
        if not deleted:
            self.index_current(item_id, kind, current_rev)
        return item_id

    def build_publications(self, data) -> None:
        groups = defaultdict(list)
        for proposal in data["proposals"]:
            groups[(proposal["project"], proposal["kind"], proposal["key"])].append(proposal)
        publications = {(p["project"], p["kind"], p["key"]): p for p in data["publications"]}
        for group in sorted(set(groups) | set(publications)):
            proposals = groups.get(group, [])
            publication = publications.get(group)
            ordered = sorted(proposals, key=lambda p: (p["submitted"], p["id"]))
            if publication is not None and publication["proposal_id"] is None:
                # Removed in v1. Proposals submitted after the removal (expected_revision at or beyond the
                # removal revision) continue as a new live item, as v1 would have treated them.
                after = [p for p in ordered if p["expected_revision"] >= publication["revision"]]
                before = [p for p in ordered if p["expected_revision"] < publication["revision"]]
                owner = (before or after or [{"author": self._fallback_owner(group[0], data)}])[0]["author"]
                if not before and not after:
                    self.notes.append(f"removed publication with no proposals; owner set to {owner}: {group!r}")
                tomb = self._project_item(group, before, current_rev=publication["revision"], deleted=True, owner=owner)
                self.map_ref(v1_ref("publications", *group), tomb)
                if after:
                    self._project_item(group, after, current_rev=0, deleted=False, owner=after[0]["author"])
                    self.notes.append(f"proposals after a removed publication imported as a new item: {group!r}")
                continue
            owner = ordered[0]["author"] if ordered else self._fallback_owner(group[0], data)
            item_id = self._project_item(group, ordered, current_rev=publication["revision"] if publication else 0,
                                         deleted=False, owner=owner)
            if publication is not None:
                self.map_ref(v1_ref("publications", *group), item_id)


def _counts(db: sqlite3.Connection, data: dict) -> dict:
    def one(sql: str) -> int:
        return db.execute(sql).fetchone()[0]

    return {
        "principals": {"in": len(data["principals"]), "out": one("SELECT COUNT(*) FROM principals"),
                       "target": "principals"},
        "projects": {"in": len(data["projects"]), "out": one("SELECT COUNT(*) FROM projects"), "target": "projects"},
        "memberships": {"in": len(data["memberships"]), "out": one("SELECT COUNT(*) FROM memberships"),
                        "target": "memberships"},
        "checkpoints": {"in": len(data["checkpoints"]),
                        "out": one("SELECT COUNT(*) FROM items WHERE kind = 'checkpoint'"), "target": "items"},
        "checkpoint_revisions": {
            "in": len(data["checkpoint_revisions"]),
            "out": one("SELECT COUNT(*) FROM item_revisions AS r JOIN items AS i ON i.id = r.item_id"
                       " WHERE i.kind = 'checkpoint'"), "target": "item_revisions"},
        "proposals": {"in": len(data["proposals"]),
                      "out": one("SELECT COUNT(*) FROM item_revisions AS r JOIN items AS i ON i.id = r.item_id"
                                 " WHERE i.kind IN ('brief', 'excerpt')"), "target": "item_revisions"},
        "publications": {"in": len(data["publications"]),
                         "out": one("SELECT COUNT(*) FROM v1_map WHERE v1_ref LIKE '[\"publications\",%'"),
                         "target": "items"},
        "receipts": {"in": len(data["receipts"]), "out": one("SELECT COUNT(*) FROM receipts WHERE scheme = 1"),
                     "target": "receipts"},
        "audit": {"in": len(data["audit"]), "out": one("SELECT COUNT(*) FROM audit WHERE legacy = 1"),
                  "target": "audit"},
    }


def migrate_v1(source, dest, *, clock: Optional[Callable[[], int]] = None, dry_run: bool = False) -> dict:
    """Build a new v2 store at dest from the v1 store at source. Returns the mapping report."""
    check_engine()
    source = Path(source).expanduser().absolute()
    dest = Path(dest).expanduser().absolute()
    if not source.is_file():
        raise RecallError("wrong_store", "The v1 store does not exist.")
    if not dry_run and (os.path.lexists(dest) or os.path.lexists(report_path(dest))):
        raise RecallError("backup_exists", "The destination already exists.")
    check_private(dest.parent, directory=True)
    source_hash = file_sha256(source)
    version, data = read_v1(source)
    mt = (clock or system_clock)()

    fd, staged_name = tempfile.mkstemp(prefix=".recall-migrate-", dir=dest.parent)
    os.close(fd)
    staged = Path(staged_name)
    report: dict = {
        "format": "recall.v2.migration-report/1", "source": str(source), "source_sha256": source_hash,
        "source_user_version": version, "destination": str(dest), "migrated_at": timefmt.format_rfc3339(mt),
        "dry_run": dry_run,
    }
    try:
        os.chmod(staged, 0o600)
        store = Store.open(staged, clock=lambda: mt)
        try:
            builder = _Builder(store, mt)
            try:
                with write_transaction(store.db) as db:
                    builder.build(data)
                    audit.append(db, at=mt, actor="~operator", action="migrate")
                    if not dry_run:
                        db.execute("INSERT INTO artifacts (path, kind, created_at, expires_at) VALUES (?, 'migration', ?, ?)",
                                   (str(report_path(dest)), mt, mt + RECEIPT_TTL_MICROS))
            except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                raise RecallError("migration_failed", "The migration was rolled back.",
                                  reason=f"{type(exc).__name__}: {exc}") from None
            tables = _counts(store.db, data)
            fk = [tuple(row) for row in store.db.execute("PRAGMA foreign_key_check")]
            integrity = store.db.execute("PRAGMA integrity_check").fetchone()[0]
            chain = audit.verify_chain(store.db)
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            store.close()
        unmapped = builder.unmapped + [
            {"table": name, "reason": f"{counts['in']} in, {counts['out']} out"}
            for name, counts in tables.items() if counts["in"] != counts["out"]]
        report.update({
            "tables": tables, "unmapped": unmapped, "notes": builder.notes,
            "checks": {"foreign_key_check": fk, "integrity_check": integrity, "audit_chain": chain},
            "v1_unchanged": file_sha256(source) == source_hash,
        })
        if unmapped or fk or integrity != "ok" or not chain["ok"] or not report["v1_unchanged"]:
            raise RecallError("migration_failed", "The migration did not map every v1 row cleanly.", report=report)
        if dry_run:
            return report
        try:
            os.link(staged, dest)
        except FileExistsError:
            raise RecallError("backup_exists", "The destination already exists.") from None
        try:
            fd = os.open(report_path(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            os.unlink(dest)
            raise RecallError("backup_exists", "The report destination already exists.") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return report
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(str(staged) + suffix)
            except FileNotFoundError:
                pass
