"""SQLite repository: principals, projects, revisions, facts, receipts, review, deletion and scoped reads."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Callable, Optional

from ..core import model, normalise, timefmt, validity
from ..core.errors import RecallError, invalid
from ..governance import dedupe
from . import audit, embeddings, receipts
from .connection import (check_engine, check_private, create_file, initialise, raw_connect, read_transaction,
                         write_transaction)
from .visibility import visible_items, visible_revisions

Clock = Callable[[], int]


def system_clock() -> int:
    return time.time_ns() // 1000


def token_digest(token: str) -> bytes:
    return sha256(token.encode("ascii")).digest()


def new_token() -> str:
    """256-bit random bearer token, URL-safe base64 (43 characters)."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Principal:
    """An authenticated handle. Each operation re-checks the digest, so revocation applies immediately."""
    id: str
    token_digest: bytes


REVISION_COLUMNS = (
    "i.id AS item_id, i.kind, i.scope_kind, i.owner_id, i.project_id, i.key, i.current_rev, i.deleted_at,"
    " r.rev, r.state, r.title, r.body, r.meta_json, r.valid_from, r.valid_to, r.actor_id, r.recorded_at,"
    " r.accepted_at, r.reviewed_by, r.reviewed_at, f.op, f.subject, f.predicate, f.value_json, f.supersedes_rev"
)
REVISION_FROM = (
    "items AS i JOIN item_revisions AS r ON r.item_id = i.id"
    " LEFT JOIN fact_versions AS f ON f.item_id = r.item_id AND f.rev = r.rev"
)
CURRENT_FACT_REV = (
    "(SELECT cr.rev FROM item_revisions AS cr WHERE cr.item_id = i.id AND cr.state = 'accepted'"
    " AND cr.accepted_at <= :tk AND cr.valid_from <= :tv AND (cr.valid_to IS NULL OR :tv < cr.valid_to)"
    " ORDER BY cr.accepted_at DESC, cr.rev DESC LIMIT 1)"
)
CURRENT_OTHER_REV = (
    "(SELECT cr.rev FROM item_revisions AS cr WHERE cr.item_id = i.id AND cr.state = 'accepted'"
    " AND cr.accepted_at <= :tk ORDER BY cr.rev DESC LIMIT 1)"
)


def _ts(value: Optional[int]) -> Optional[str]:
    return None if value is None else timefmt.format_rfc3339(value)


def revision_dict(row: sqlite3.Row) -> dict:
    fact = None
    if row["kind"] == "fact" and row["op"] is not None:
        fact = {"subject": row["subject"], "predicate": row["predicate"],
                "value": None if row["value_json"] is None else json.loads(row["value_json"]),
                "op": row["op"], "supersedes": row["supersedes_rev"]}
    scope_id = row["owner_id"] if row["scope_kind"] == "private" else row["project_id"]
    return {
        "id": row["item_id"], "kind": row["kind"], "scope": {"type": row["scope_kind"], "id": scope_id},
        "key": None if row["kind"] == "fact" else row["key"], "rev": row["rev"], "state": row["state"],
        "title": row["title"], "body": row["body"], "fact": fact,
        "valid_from": _ts(row["valid_from"]), "valid_to": _ts(row["valid_to"]),
        "recorded_at": _ts(row["recorded_at"]), "accepted_at": _ts(row["accepted_at"]),
    }


class Store:
    """One store file. Open with Store.open(path, create=...)."""

    PIN_LIMIT = 8

    def __init__(self, db: sqlite3.Connection, path: Path, clock: Clock,
                 embedder: Optional[embeddings.Embedder] = None):
        self.db = db
        self.path = path
        self.clock = clock
        # Optional; when set, embeddings are written in each revision's transaction and lookup runs
        # the vector leg. Off by default.
        self.embedder = embedder

    # -- lifecycle -------------------------------------------------------------------------------

    @classmethod
    def open(cls, path, *, create: bool = False, clock: Optional[Clock] = None,
             embedder: Optional[embeddings.Embedder] = None) -> "Store":
        check_engine()
        path = Path(path).expanduser().absolute()
        if create and not os.path.lexists(path):
            check_private(path.parent, directory=True)
            create_file(path)
        check_private(path.parent, directory=True)
        check_private(path)
        db = raw_connect(path)
        try:
            initialise(db)
        except BaseException:
            db.close()
            raise
        return cls(db, path, clock or system_clock, embedder)

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _ulid(self, now: int) -> str:
        return model.encode_ulid(max(now // 1000, 0) % (2 ** 48), secrets.token_bytes(10))

    # -- principals, projects, memberships (operator) --------------------------------------------

    def principal_add(self, principal_id: str) -> str:
        """Create a principal; return its token once. Only the SHA-256 digest is stored."""
        normalise.check_id(principal_id, normalise.PRINCIPAL_ID, "principal")
        token = new_token()
        with write_transaction(self.db) as db:
            now = self.clock()
            if db.execute("SELECT 1 FROM principals WHERE id = ?", (principal_id,)).fetchone():
                raise RecallError("conflict", "The principal exists.")
            db.execute("INSERT INTO principals (id, token_digest, created_at) VALUES (?, ?, ?)",
                       (principal_id, token_digest(token), now))
            audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="principal_add")
        return token

    def principal_rotate(self, principal_id: str) -> str:
        token = new_token()
        with write_transaction(self.db) as db:
            now = self.clock()
            if not db.execute("UPDATE principals SET token_digest = ?, revoked_at = NULL WHERE id = ?",
                              (token_digest(token), principal_id)).rowcount:
                raise RecallError("not_found", "No such principal.")
            audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="principal_rotate")
        return token

    def principal_revoke(self, principal_id: str) -> None:
        with write_transaction(self.db) as db:
            now = self.clock()
            if not db.execute("UPDATE principals SET token_digest = NULL, revoked_at = ? WHERE id = ?",
                              (now, principal_id)).rowcount:
                raise RecallError("not_found", "No such principal.")
            audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="principal_revoke")

    def project_add(self, project_id: str) -> None:
        normalise.check_id(project_id, normalise.PROJECT_ID, "project")
        with write_transaction(self.db) as db:
            now = self.clock()
            if db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
                raise RecallError("conflict", "The project exists.")
            db.execute("INSERT INTO projects (id, created_at) VALUES (?, ?)", (project_id, now))
            audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="project_add", project_id=project_id)

    def member_add(self, principal_id: str, project_id: str) -> None:
        with write_transaction(self.db) as db:
            now = self.clock()
            if (db.execute("SELECT 1 FROM principals WHERE id = ?", (principal_id,)).fetchone() is None
                    or db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None):
                raise RecallError("not_found", "No such principal or project.")
            if db.execute("INSERT OR IGNORE INTO memberships (principal_id, project_id, added_at) VALUES (?, ?, ?)",
                          (principal_id, project_id, now)).rowcount:
                audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="member_add", project_id=project_id)

    def member_remove(self, principal_id: str, project_id: str) -> None:
        with write_transaction(self.db) as db:
            now = self.clock()
            if db.execute("DELETE FROM memberships WHERE principal_id = ? AND project_id = ?",
                          (principal_id, project_id)).rowcount:
                audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="member_remove",
                             project_id=project_id)

    def authenticate(self, token: str) -> Principal:
        if not isinstance(token, str) or not token.isascii():
            raise RecallError("unauthorized", "A current token is required.")
        digest = token_digest(token)
        with read_transaction(self.db) as db:
            return Principal(self._caller(db, digest), digest)

    @staticmethod
    def _caller(db: sqlite3.Connection, digest: bytes) -> str:
        row = db.execute("SELECT id FROM principals WHERE token_digest = ? AND revoked_at IS NULL",
                         (digest,)).fetchone()
        if row is None:
            raise RecallError("unauthorized", "A current token is required.")
        return row["id"]

    # -- writes ----------------------------------------------------------------------------------

    def _fact_history(self, db, item_id: str) -> list[tuple[validity.FactRevision, Optional[str]]]:
        rows = db.execute(
            "SELECT r.rev, r.state, r.valid_from, r.valid_to, r.accepted_at, f.op, f.value_json"
            " FROM item_revisions AS r JOIN fact_versions AS f USING (item_id, rev) WHERE r.item_id = ?"
            " ORDER BY r.rev", (item_id,)).fetchall()
        return [(validity.FactRevision(row["rev"], row["op"], row["valid_from"], row["valid_to"], row["state"],
                                       row["accepted_at"]), row["value_json"]) for row in rows]

    def _fts_index(self, db, item_id: str, rev: int, kind: str) -> None:
        """FTS covers the current accepted revision of non-fact items and every accepted fact revision."""
        if kind != "fact":
            db.execute("DELETE FROM fts_items WHERE rowid IN (SELECT rowid FROM item_revisions WHERE item_id = ?)",
                       (item_id,))
        row = db.execute(
            "SELECT r.rowid AS rid, r.title, r.body, f.subject, f.predicate, f.value_json FROM item_revisions AS r"
            " LEFT JOIN fact_versions AS f USING (item_id, rev) WHERE r.item_id = ? AND r.rev = ?",
            (item_id, rev)).fetchone()
        if kind == "fact":
            title, body = f"{row['subject']} | {row['predicate']}", row["value_json"]
        else:
            title, body = row["title"], row["body"]
        db.execute("INSERT INTO fts_items (rowid, title, body) VALUES (?, ?, ?)", (row["rid"], title or "", body or ""))
        if self.embedder is not None:
            self._embed_index(db, item_id, rev, kind, title or "", body or "")

    def _embed_index(self, db, item_id: str, rev: int, kind: str, title: str, body: str) -> None:
        """Embeddings follow the FTS rule: current revision for non-facts, every accepted fact revision."""
        model_name, dim = self.embedder.model, self.embedder.dim
        if kind != "fact":
            db.execute("DELETE FROM embeddings WHERE item_id = ? AND model = ?", (item_id, model_name))
        [vector] = self.embedder.embed([embeddings.document_text(title, body)])
        db.execute("INSERT OR REPLACE INTO embeddings (item_id, rev, model, dim, vector) VALUES (?, ?, ?, ?, ?)",
                   (item_id, rev, model_name, dim, embeddings.encode(vector, dim)))

    def _member(self, db, principal_id: str, project_id: str) -> bool:
        return db.execute("SELECT 1 FROM memberships WHERE principal_id = ? AND project_id = ?",
                          (principal_id, project_id)).fetchone() is not None

    def save(self, principal: Principal, *, kind, request_id, scope="private", project=None, key=None,
             title=None, body=None, fact=None, valid_from=None, valid_to=None, expected_revision=None,
             source_ref=None) -> dict:
        """Write one revision (or return an existing one). Returns {"item_id", "rev"}."""
        normalise.check_id(request_id, normalise.REQUEST_ID, "request_id")
        req = model.validate_save(kind=kind, scope=scope, project=project, key=key, title=title, body=body,
                                  fact=fact, valid_from=valid_from, valid_to=valid_to,
                                  expected_revision=expected_revision, source_ref=source_ref)
        digest = receipts.fingerprint(req.fingerprint_payload())
        with write_transaction(self.db) as db:
            now = self.clock()
            caller = self._caller(db, principal.token_digest)
            previous = receipts.replay(db, caller, request_id, digest, now)
            if previous is not None:
                return previous
            result = self._save(db, caller, req, request_id, now)
            receipts.record(db, caller, request_id, digest, result["item_id"], result["rev"], now)
            return result

    def _find_item(self, db, caller: str, req: model.SaveRequest) -> Optional[sqlite3.Row]:
        if req.scope == "private":
            return db.execute("SELECT * FROM items WHERE scope_kind = 'private' AND owner_id = ? AND kind = ?"
                              " AND key = ?", (caller, req.kind, req.key)).fetchone()
        return db.execute("SELECT * FROM items WHERE scope_kind = 'project' AND project_id = ? AND kind = ?"
                          " AND key = ?", (req.project, req.kind, req.key)).fetchone()

    def _exact_duplicate(self, db, caller: str, item_id: str, req: model.SaveRequest, state: str,
                         hashed: Optional[bytes], now: int) -> Optional[dict]:
        """Section 3.3 exact dedupe: the item's latest revision of the same state, if its normalised body
        hash equals the new body's. Facts carry no body, so a fact save always writes a revision (a
        re-assertion moves valid_from and accepted_at, which current truth depends on).

        Only revisions visible to the caller are compared, so another author's candidate is never
        returned (or revealed) to the caller.
        """
        if req.kind == "fact":
            return None
        latest = db.execute(
            f"SELECT r.rev, r.body_hash FROM {REVISION_FROM} WHERE i.id = :id AND r.state = :state"
            f" AND {visible_revisions('r', 'i')} ORDER BY r.rev DESC LIMIT 1",
            {"id": item_id, "state": state, "caller": caller}).fetchone()
        if latest is not None and latest["body_hash"] == hashed:
            return {"item_id": item_id, "rev": latest["rev"]}
        return None

    def _save(self, db, caller: str, req: model.SaveRequest, request_id: Optional[str], now: int, *,
              actor: Optional[str] = None, candidate_only: bool = False) -> dict:
        """Write one revision for `caller` (the item owner). `actor` defaults to the caller; extraction
        passes its own actor ID and candidate_only=True, so its output is never accepted on write."""
        actor = actor or caller
        if req.scope == "project" and not self._member(db, caller, req.project):
            raise RecallError("not_found", "No such project.")
        item = self._find_item(db, caller, req)
        current_rev = item["current_rev"] if item else 0
        if req.expected_revision is not None and req.expected_revision != current_rev:
            raise RecallError("conflict", "The revision has changed.", current_revision=current_rev)
        state = "candidate" if candidate_only else model.state_on_write(req.scope)
        hashed = None if req.body is None else normalise.body_hash(req.body)

        if item is not None:
            duplicate = self._exact_duplicate(db, caller, item["id"], req, state, hashed, now)
            if duplicate is not None:
                return duplicate

        if item is None:
            item_id = self._ulid(now)
            db.execute(
                "INSERT INTO items (id, kind, scope_kind, owner_id, project_id, key, current_rev, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (item_id, req.kind, req.scope, caller, req.project, req.key, now))
        else:
            item_id = item["id"]
        rev = (db.execute("SELECT COALESCE(MAX(rev), 0) FROM item_revisions WHERE item_id = ?",
                          (item_id,)).fetchone()[0]) + 1
        if rev > model.MAX_REV:
            raise invalid("The item has too many revisions.")

        valid_from = req.valid_from if req.valid_from is not None else now
        history = self._fact_history(db, item_id) if req.kind == "fact" else []
        valid_to = req.valid_to
        if req.kind == "fact" and valid_to is None:
            valid_to = validity.default_valid_to(valid_from, [h for h, _ in history])
        validity.check_interval(valid_from, valid_to)

        meta = {"base_rev": current_rev}
        if req.source_ref is not None:
            meta["source_ref"] = req.source_ref
        accepted_at = now if state == "accepted" else None
        db.execute(
            "INSERT INTO item_revisions (item_id, rev, state, title, body, body_hash, meta_json, valid_from, valid_to,"
            " actor_id, request_id, recorded_at, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, rev, state, req.title, req.body, hashed, normalise.canonical_json(meta), valid_from, valid_to,
             actor, request_id, now, accepted_at))

        if req.kind == "fact":
            revisions = [h for h, _ in history]
            superseded = validity.current_fact(revisions, valid_from, now)
            db.execute(
                "INSERT INTO fact_versions (item_id, rev, op, subject, predicate, value_json, supersedes_rev)"
                " VALUES (?, ?, 'assert', ?, ?, ?, ?)",
                (item_id, rev, req.fact.subject, req.fact.predicate, req.fact.value_json,
                 superseded.rev if superseded else None))
            status = "auto_resolved" if state == "accepted" else "open"
            for existing, value_json in history:
                if validity.conflicts_with(valid_from, valid_to, req.fact.value_json, existing, value_json, revisions):
                    db.execute("INSERT INTO conflicts (id, item_id, rev_a, rev_b, status) VALUES (?, ?, ?, ?, ?)",
                               (self._ulid(now), item_id, existing.rev, rev, status))

        if state == "candidate":
            if req.kind == "fact":
                text = embeddings.document_text(f"{req.fact.subject} | {req.fact.predicate}", req.fact.value_json)
            else:
                text = embeddings.document_text(req.title or "", req.body or "")
            dedupe.flag_near_duplicates(db, self.embedder, caller=caller, item_id=item_id, rev=rev, kind=req.kind,
                                        scope_kind=req.scope, owner_id=caller, project_id=req.project, text=text,
                                        now=now)

        if state == "accepted":
            db.execute("UPDATE items SET current_rev = MAX(current_rev, ?) WHERE id = ?", (rev, item_id))
            self._fts_index(db, item_id, rev, req.kind)
        audit.append(db, at=now, actor=actor, action="save", item_id=item_id, rev=rev, project_id=req.project)
        return {"item_id": item_id, "rev": rev}

    def retract(self, principal: Principal, *, item_id, rev, request_id, valid_from=None, valid_to=None) -> dict:
        """Insert a retract revision. Its interval defaults to the retracted revision's effective interval."""
        normalise.check_id(request_id, normalise.REQUEST_ID, "request_id")
        if isinstance(rev, bool) or not isinstance(rev, int):
            raise invalid("rev must be an integer.")
        valid_from, valid_to = timefmt.coerce(valid_from), timefmt.coerce(valid_to)
        digest = receipts.fingerprint({"op": "retract", "item_id": item_id, "rev": rev,
                                       "valid_from": valid_from, "valid_to": valid_to})
        with write_transaction(self.db) as db:
            now = self.clock()
            caller = self._caller(db, principal.token_digest)
            previous = receipts.replay(db, caller, request_id, digest, now)
            if previous is not None:
                return previous
            item = db.execute(f"SELECT * FROM items AS i WHERE i.id = :id AND {visible_items('i')}",
                              {"id": item_id, "caller": caller}).fetchone()
            if item is None:
                raise RecallError("not_found", "No such item.")
            if item["kind"] != "fact":
                raise invalid("Only facts can be retracted.")
            history = self._fact_history(db, item_id)
            revisions = [h for h, _ in history]
            target = next((h for h in revisions if h.rev == rev), None)
            if target is None or target.state != "accepted" or target.op != validity.ASSERT:
                raise RecallError("not_found", "No such accepted assert revision.")
            eff_from, eff_to = validity.effective_interval(target, revisions)
            start = valid_from if valid_from is not None else eff_from
            end = valid_to if valid_to is not None else eff_to
            validity.check_interval(start, end)
            new_rev = max(h.rev for h in revisions) + 1
            if new_rev > model.MAX_REV:
                raise invalid("The item has too many revisions.")
            state = model.state_on_write(item["scope_kind"])
            accepted_at = now if state == "accepted" else None
            fact_row = db.execute("SELECT subject, predicate, value_json FROM fact_versions WHERE item_id = ? AND rev = ?",
                                  (item_id, rev)).fetchone()
            db.execute(
                "INSERT INTO item_revisions (item_id, rev, state, meta_json, valid_from, valid_to, actor_id, request_id,"
                " recorded_at, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, new_rev, state, normalise.canonical_json({"base_rev": item["current_rev"]}), start, end,
                 caller, request_id, now, accepted_at))
            db.execute(
                "INSERT INTO fact_versions (item_id, rev, op, subject, predicate, value_json, supersedes_rev)"
                " VALUES (?, ?, 'retract', ?, ?, ?, ?)",
                (item_id, new_rev, fact_row["subject"], fact_row["predicate"], fact_row["value_json"], rev))
            if state == "accepted":
                db.execute("UPDATE items SET current_rev = MAX(current_rev, ?) WHERE id = ?", (new_rev, item_id))
                self._fts_index(db, item_id, new_rev, "fact")
            audit.append(db, at=now, actor=caller, action="retract", item_id=item_id, rev=new_rev,
                         project_id=item["project_id"])
            result = {"item_id": item_id, "rev": new_rev}
            receipts.record(db, caller, request_id, digest, item_id, new_rev, now)
            return result

    # -- pins ------------------------------------------------------------------------------------

    def pin(self, principal: Principal, *, item_id: str) -> dict:
        """Pin a visible item for the caller (at most PIN_LIMIT pins per principal)."""
        with write_transaction(self.db) as db:
            now = self.clock()
            caller = self._caller(db, principal.token_digest)
            item = db.execute(f"SELECT id, project_id FROM items AS i WHERE i.id = :id AND {visible_items('i')}",
                              {"id": item_id, "caller": caller}).fetchone()
            if item is None:
                raise RecallError("not_found", "No such item.")
            if db.execute("SELECT 1 FROM pins WHERE principal_id = ? AND item_id = ?", (caller, item_id)).fetchone():
                return {"item_id": item_id, "pinned": True}
            if db.execute("SELECT COUNT(*) FROM pins WHERE principal_id = ?", (caller,)).fetchone()[0] >= self.PIN_LIMIT:
                raise RecallError("pin_limit", "The principal already has 8 pins.")
            db.execute("INSERT INTO pins (principal_id, item_id) VALUES (?, ?)", (caller, item_id))
            audit.append(db, at=now, actor=caller, action="pin", item_id=item_id, project_id=item["project_id"])
            return {"item_id": item_id, "pinned": True}

    def unpin(self, principal: Principal, *, item_id: str) -> dict:
        with write_transaction(self.db) as db:
            now = self.clock()
            caller = self._caller(db, principal.token_digest)
            item = db.execute(f"SELECT id, project_id FROM items AS i WHERE i.id = :id AND {visible_items('i')}",
                              {"id": item_id, "caller": caller}).fetchone()
            if item is None:
                raise RecallError("not_found", "No such item.")
            if db.execute("DELETE FROM pins WHERE principal_id = ? AND item_id = ?", (caller, item_id)).rowcount:
                audit.append(db, at=now, actor=caller, action="unpin", item_id=item_id, project_id=item["project_id"])
            return {"item_id": item_id, "pinned": False}

    # -- review (operator) -----------------------------------------------------------------------

    def _review(self, item_id: str, rev: int, new_state: str, reviewer: str, note: Optional[str]) -> dict:
        with write_transaction(self.db) as db:
            return self._review_in(db, self.clock(), item_id, rev, new_state, reviewer, note)

    def review_row(self, db, item_id: str, rev: int) -> sqlite3.Row:
        """The item and revision fields review needs; not_found for a missing or deleted item."""
        row = db.execute(
            "SELECT i.kind, i.scope_kind, i.owner_id, i.project_id, i.current_rev, i.deleted_at, r.rev, r.state,"
            " r.actor_id, r.meta_json, r.reviewed_at FROM items AS i JOIN item_revisions AS r ON r.item_id = i.id"
            " WHERE i.id = ? AND r.rev = ?", (item_id, rev)).fetchone()
        if row is None or row["deleted_at"] is not None:
            raise RecallError("not_found", "No such revision.")
        if row["state"] != "candidate" and row["reviewed_at"] is not None:
            raise RecallError("stale_review", "The revision was already reviewed.")
        return row

    def check_approval_basis(self, db, row: sqlite3.Row) -> None:
        """Section 3.6: stale_review if current_rev changed since the candidate was written, or its
        principal author is no longer a member (project scope) or is revoked. Candidates written by the
        operator or by extraction (actor IDs starting with "~") have no principal to re-check."""
        base = json.loads(row["meta_json"] or "{}").get("base_rev", row["current_rev"])
        if row["current_rev"] != base:
            raise RecallError("stale_review", "The approval basis changed.", current_revision=row["current_rev"])
        if row["actor_id"].startswith("~"):
            return
        author = db.execute("SELECT revoked_at FROM principals WHERE id = ?", (row["actor_id"],)).fetchone()
        if (author is None or author["revoked_at"] is not None
                or (row["project_id"] is not None and not self._member(db, row["actor_id"], row["project_id"]))):
            raise RecallError("stale_review", "The approval basis changed.")

    def _review_in(self, db, now: int, item_id: str, rev: int, new_state: str, reviewer: str,
                   note: Optional[str]) -> dict:
        """Approve or reject one candidate inside the caller's IMMEDIATE transaction.

        Approval is a compare-and-swap on the candidate's base revision (check_approval_basis). Open
        conflicts on the revision are closed as `resolved` by this explicit operator action, each with
        its own audit event; nothing else ever closes an open conflict.
        """
        row = self.review_row(db, item_id, rev)
        model.check_transition(row["state"], new_state)
        if new_state == "accepted":
            self.check_approval_basis(db, row)
        db.execute(
            "UPDATE item_revisions SET state = ?, accepted_at = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?"
            " WHERE item_id = ? AND rev = ?",
            (new_state, now if new_state == "accepted" else None, reviewer, now, note, item_id, rev))
        if new_state == "accepted":
            db.execute("UPDATE items SET current_rev = MAX(current_rev, ?) WHERE id = ?", (rev, item_id))
            self._fts_index(db, item_id, rev, row["kind"])
        audit.append(db, at=now, actor=model.OPERATOR_ACTOR,
                     action="review_approve" if new_state == "accepted" else "review_reject",
                     item_id=item_id, rev=rev, project_id=row["project_id"])
        for conflict in db.execute("SELECT id FROM conflicts WHERE item_id = ? AND rev_b = ? AND status = 'open'"
                                   " ORDER BY id", (item_id, rev)).fetchall():
            db.execute("UPDATE conflicts SET status = 'resolved', resolved_by = ?, resolved_at = ? WHERE id = ?",
                       (reviewer, now, conflict["id"]))
            audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="conflict_resolve", item_id=item_id,
                         rev=rev, project_id=row["project_id"])
        return {"item_id": item_id, "rev": rev, "state": new_state}

    def review_approve(self, item_id: str, rev: int, *, reviewer: str = model.OPERATOR_ACTOR) -> dict:
        return self._review(item_id, rev, "accepted", reviewer, None)

    def review_reject(self, item_id: str, rev: int, *, reviewer: str = model.OPERATOR_ACTOR,
                      note: Optional[str] = None) -> dict:
        return self._review(item_id, rev, "rejected", reviewer, note)

    def delete_item(self, item_id: str) -> dict:
        """Operator deletion: null content, tombstone the item, drop FTS rows, receipts and pins, record an erasure."""
        with write_transaction(self.db) as db:
            now = self.clock()
            item = db.execute("SELECT * FROM items WHERE id = ? AND deleted_at IS NULL", (item_id,)).fetchone()
            if item is None:
                raise RecallError("not_found", "No such item.")
            db.execute("DELETE FROM fts_items WHERE rowid IN (SELECT rowid FROM item_revisions WHERE item_id = ?)",
                       (item_id,))
            db.execute("UPDATE item_revisions SET title = NULL, body = NULL, meta_json = NULL, body_hash = NULL"
                       " WHERE item_id = ?", (item_id,))
            db.execute("UPDATE fact_versions SET subject = NULL, predicate = NULL, value_json = NULL WHERE item_id = ?",
                       (item_id,))
            db.execute("UPDATE items SET key = NULL, deleted_at = ? WHERE id = ?", (now, item_id))
            db.execute("DELETE FROM receipts WHERE item_id = ?", (item_id,))
            db.execute("DELETE FROM pins WHERE item_id = ?", (item_id,))
            db.execute("DELETE FROM embeddings WHERE item_id = ?", (item_id,))
            last_rev = db.execute("SELECT COALESCE(MAX(rev), 0) FROM item_revisions WHERE item_id = ?",
                                  (item_id,)).fetchone()[0]
            db.execute("INSERT OR REPLACE INTO erasures (item_id, rev, at) VALUES (?, ?, ?)", (item_id, last_rev, now))
            audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="delete", item_id=item_id, rev=last_rev,
                         project_id=item["project_id"])
            return {"item_id": item_id, "rev": last_rev}

    # -- reads (all through visible_items / visible_revisions) -----------------------------------

    def _times(self, as_of, known_at) -> tuple[int, int]:
        now = self.clock()
        tv = timefmt.coerce(as_of)
        tk = timefmt.coerce(known_at)
        return (now if tv is None else tv), (now if tk is None else tk)

    def get(self, principal: Principal, *, item_id: str, rev: Optional[int] = None, as_of=None,
            known_at=None) -> dict:
        tv, tk = self._times(as_of, known_at)
        with read_transaction(self.db) as db:
            caller = self._caller(db, principal.token_digest)
            params = {"caller": caller, "id": item_id, "tv": tv, "tk": tk, "rev": rev}
            if rev is not None:
                where = f"i.id = :id AND r.rev = :rev AND {visible_items('i')} AND {visible_revisions('r', 'i')}"
                row = db.execute(f"SELECT {REVISION_COLUMNS} FROM {REVISION_FROM} WHERE {where}", params).fetchone()
            else:
                row = db.execute(
                    f"SELECT {REVISION_COLUMNS} FROM {REVISION_FROM} WHERE i.id = :id AND {visible_items('i')}"
                    f" AND r.rev = (CASE WHEN i.kind = 'fact' THEN {CURRENT_FACT_REV} ELSE {CURRENT_OTHER_REV} END)"
                    " AND (i.kind <> 'fact' OR f.op = 'assert')"
                    " AND r.valid_from <= :tv AND (r.valid_to IS NULL OR :tv < r.valid_to)", params).fetchone()
            if row is None:
                raise RecallError("not_found", "No such item.")
            return revision_dict(row)

    def history(self, principal: Principal, *, item_id: str) -> list[dict]:
        now = self.clock()
        with read_transaction(self.db) as db:
            caller = self._caller(db, principal.token_digest)
            params = {"caller": caller, "id": item_id, "tv": now, "tk": now}
            rows = db.execute(
                f"SELECT {REVISION_COLUMNS},"
                f" (CASE WHEN i.kind = 'fact' THEN {CURRENT_FACT_REV} ELSE {CURRENT_OTHER_REV} END) AS winner"
                f" FROM {REVISION_FROM} WHERE i.id = :id AND {visible_items('i')} AND {visible_revisions('r', 'i')}"
                " ORDER BY r.rev", params).fetchall()
            if not rows:
                raise RecallError("not_found", "No such item.")
            out = []
            for row in rows:
                entry = revision_dict(row)
                entry["current"] = (row["rev"] == row["winner"] and (row["kind"] != "fact" or row["op"] == "assert")
                                    and validity.contains(row["valid_from"], row["valid_to"], now))
                out.append(entry)
            return out

    def lookup_facts(self, principal: Principal, *, subject: Optional[str] = None, predicate: Optional[str] = None,
                     scope: Optional[str] = None, project: Optional[str] = None, as_of=None,
                     known_at=None) -> list[dict]:
        """Fact mode: the current revision per matching visible fact key (at most one per key)."""
        if subject is None and predicate is None:
            raise invalid("subject or predicate is required.")
        if scope not in (None, "private", "project"):
            raise invalid("scope must be private or project.")
        tv, tk = self._times(as_of, known_at)
        params = {"tv": tv, "tk": tk, "scope": scope, "project": project,
                  "subject": None if subject is None else normalise.norm(subject),
                  "predicate": None if predicate is None else normalise.norm(predicate)}
        with read_transaction(self.db) as db:
            params["caller"] = self._caller(db, principal.token_digest)
            rows = db.execute(
                f"SELECT {REVISION_COLUMNS} FROM {REVISION_FROM}"
                f" WHERE i.kind = 'fact' AND {visible_items('i')} AND r.rev = {CURRENT_FACT_REV} AND f.op = 'assert'"
                " AND (:scope IS NULL OR i.scope_kind = :scope) AND (:project IS NULL OR i.project_id = :project)"
                " AND (:subject IS NULL OR substr(i.key, 1, instr(i.key, char(31)) - 1) = :subject)"
                " AND (:predicate IS NULL OR substr(i.key, instr(i.key, char(31)) + 1) = :predicate)"
                " ORDER BY (i.scope_kind = 'project'), i.project_id, i.key", params).fetchall()
            return [revision_dict(row) for row in rows]

    def visible_item_ids(self, principal: Principal) -> list[str]:
        """Every item ID in the caller's visible set (used by counts and the leak tests)."""
        with read_transaction(self.db) as db:
            caller = self._caller(db, principal.token_digest)
            return [row[0] for row in db.execute(
                f"SELECT i.id FROM items AS i WHERE {visible_items('i')} AND EXISTS (SELECT 1 FROM item_revisions AS r"
                f" WHERE r.item_id = i.id AND {visible_revisions('r', 'i')}) ORDER BY i.id", {"caller": caller})]

    def verify_audit(self) -> dict:
        with read_transaction(self.db) as db:
            return audit.verify_chain(db)
