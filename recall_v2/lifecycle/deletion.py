"""Deletion (section 6) and source deletion (section 3.7).

Operator `delete` erases any live item; a principal's `forget` erases items in its own private scope and
project items made only of candidates it authored (anything else visible is `forbidden`; invisible is
`not_found`). Both run store.erasure in one IMMEDIATE transaction and return the tombstone ITEM together
with what deletion does not guarantee. The operator result also lists the registered backups and exports
that predate the deletion (they still hold the content until they expire and are pruned).
"""
from __future__ import annotations

from ..core import model, normalise, timefmt
from ..core.errors import RecallError
from ..governance.authority import require_operator
from ..retrieval.candidates import COLUMNS, enrich
from ..store import audit, erasure, receipts
from ..store.connection import write_transaction
from ..store.repository import REVISION_FROM, Principal
from ..store.visibility import visible_items, visible_revisions

NOT_GUARANTEED = (
    "Removal from backups and exports made before this deletion, until they expire and are pruned.",
    "Removal of content already sent to an extraction endpoint.",
    "Removal from OS snapshots, swap or SSD remnants.",
    "Removal of copies in agent transcripts, caches or logs.",
    "Removal of traces in the live store file before maintenance runs.",
)


def tombstone(db, caller: str, item_id: str) -> dict:
    """The ITEM of a deleted item's last revision: identifiers, kind, scope and revision remain; content is null."""
    row = db.execute(f"SELECT {COLUMNS} FROM {REVISION_FROM} WHERE i.id = ? ORDER BY r.rev DESC LIMIT 1",
                     (item_id,)).fetchone()
    item = enrich(db, caller, row)
    item["deleted_at"] = timefmt.format_rfc3339(row["deleted_at"])
    return item


def predating_artifacts(db, now: int) -> list[dict]:
    return [{"path": r["path"], "kind": r["kind"], "created_at": timefmt.format_rfc3339(r["created_at"]),
             "expires_at": timefmt.format_rfc3339(r["expires_at"])}
            for r in db.execute("SELECT path, kind, created_at, expires_at FROM artifacts"
                                " WHERE kind IN ('backup', 'export') AND created_at <= ? ORDER BY created_at, path",
                                (now,))]


def delete(session, item_id: str) -> dict:
    """Operator deletion. Returns {"tombstone", "not_guaranteed", "artifacts"}."""
    store = require_operator(session).store
    with write_transaction(store.db) as db:
        now = store.clock()
        erasure.erase_in(db, item_id, now, model.OPERATOR_ACTOR, store.ledger_path)
        return {"tombstone": tombstone(db, model.OPERATOR_ACTOR, item_id), "not_guaranteed": list(NOT_GUARANTEED),
                "artifacts": predating_artifacts(db, now)}


def forget(store, principal: Principal, *, item_id: str, request_id: str) -> dict:
    """A principal's deletion. Returns {"tombstone", "not_guaranteed", "artifacts_predating"} (a count:
    artifact paths are operator information)."""
    normalise.check_id(request_id, normalise.REQUEST_ID, "request_id")
    if not isinstance(item_id, str):
        raise RecallError("invalid_input", "item_id must be a string.")
    digest = receipts.fingerprint({"op": "forget", "item_id": item_id})
    with write_transaction(store.db) as db:
        now = store.clock()
        caller = store._caller(db, principal.token_digest)
        previous = receipts.replay(db, caller, request_id, digest, now)
        if previous is None:
            item = db.execute(
                f"SELECT * FROM items AS i WHERE i.id = :id AND {visible_items('i')} AND EXISTS ("
                f"SELECT 1 FROM item_revisions AS r WHERE r.item_id = i.id AND {visible_revisions('r', 'i')})",
                {"id": item_id, "caller": caller}).fetchone()
            visible_revs = [] if item is None else db.execute(
                "SELECT state, actor_id FROM item_revisions WHERE item_id = ?", (item_id,)).fetchall()
            if item is None:   # absent, or nothing in it the caller may see: indistinguishable
                raise RecallError("not_found", "No such item.")
            own_private = item["scope_kind"] == "private" and item["owner_id"] == caller
            own_candidates = (item["scope_kind"] == "project" and visible_revs
                              and all(r["state"] != "accepted" and r["actor_id"] == caller for r in visible_revs))
            if not (own_private or own_candidates):
                raise RecallError("forbidden", "Only items you own or candidates you authored can be forgotten.")
            result = erasure.erase_in(db, item_id, now, caller, store.ledger_path)
            receipts.record(db, caller, request_id, digest, item_id, result["rev"], now)
        elif db.execute("SELECT deleted_at FROM items WHERE id = ?", (item_id,)).fetchone()["deleted_at"] is None:
            raise RecallError("not_found", "No such item.")
        return {"tombstone": tombstone(db, caller, item_id), "not_guaranteed": list(NOT_GUARANTEED),
                "artifacts_predating": len(predating_artifacts(db, now))}


def source_delete(session, source_id: str, *, cascade: bool = False) -> dict:
    """Section 3.7. Null the source's URI and adapter configuration (ID and content hash stay), mark it
    deleted and disable extraction for it. Candidates whose every source is now deleted are rejected
    (review_note `source_deleted`) and erased: an item left with no accepted revision and no open
    candidate is erased as a whole (tombstone, ledger); otherwise the rejected revisions' content is
    nulled. Candidates with another live source stay. Accepted items stay and show `deleted: true` on the
    source, unless cascade=True, which erases every live item with an accepted revision from the source.
    """
    store = require_operator(session).store
    if not isinstance(cascade, bool):
        raise RecallError("invalid_input", "cascade must be a boolean.")
    with write_transaction(store.db) as db:
        now = store.clock()
        if db.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
            raise RecallError("not_found", "No such source.")
        db.execute("UPDATE sources SET uri = NULL, status = 'deleted' WHERE id = ?", (source_id,))
        db.execute("UPDATE source_settings SET enabled = 0, adapter_config_json = '{}', updated_at = ?"
                   " WHERE source_id = ?", (now, source_id))
        project = db.execute("SELECT project_id FROM source_settings WHERE source_id = ?", (source_id,)).fetchone()
        audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="source_delete",
                     project_id=project["project_id"] if project else None)

        rejected, erased, nulled = [], [], []
        candidates = db.execute(
            "SELECT DISTINCT p.item_id, p.rev FROM provenance AS p"
            " JOIN item_revisions AS r ON r.item_id = p.item_id AND r.rev = p.rev"
            " JOIN items AS i ON i.id = p.item_id"
            " WHERE p.source_id = ? AND r.state = 'candidate' AND i.deleted_at IS NULL ORDER BY p.item_id, p.rev",
            (source_id,)).fetchall()
        for row in candidates:
            live = db.execute(
                "SELECT COUNT(*) FROM provenance AS p JOIN sources AS s ON s.id = p.source_id"
                " WHERE p.item_id = ? AND p.rev = ? AND s.status <> 'deleted'", (row["item_id"], row["rev"])).fetchone()[0]
            if live:
                continue
            store._review_in(db, now, row["item_id"], row["rev"], "rejected", model.OPERATOR_ACTOR, "source_deleted")
            rejected.append({"item_id": row["item_id"], "rev": row["rev"]})
        for item_id in sorted({r["item_id"] for r in rejected}):
            remaining = db.execute("SELECT COUNT(*) FROM item_revisions WHERE item_id = ?"
                                   " AND state IN ('accepted', 'candidate')", (item_id,)).fetchone()[0]
            if remaining == 0:
                erasure.erase_in(db, item_id, now, model.OPERATOR_ACTOR, store.ledger_path)
                erased.append(item_id)
            else:
                for r in rejected:
                    if r["item_id"] == item_id:
                        db.execute("DELETE FROM fts_items WHERE rowid IN (SELECT rowid FROM item_revisions"
                                   " WHERE item_id = ? AND rev = ?)", (item_id, r["rev"]))
                        erasure.null_revision(db, item_id, r["rev"])
                        nulled.append(r)
        cascaded = []
        if cascade:
            for row in db.execute(
                    "SELECT DISTINCT p.item_id FROM provenance AS p JOIN item_revisions AS r"
                    " ON r.item_id = p.item_id AND r.rev = p.rev JOIN items AS i ON i.id = p.item_id"
                    " WHERE p.source_id = ? AND r.state = 'accepted' AND i.deleted_at IS NULL ORDER BY p.item_id",
                    (source_id,)).fetchall():
                erasure.erase_in(db, row["item_id"], now, model.OPERATOR_ACTOR, store.ledger_path)
                cascaded.append(row["item_id"])
        result = {"source_id": source_id, "rejected": rejected, "erased_items": erased, "nulled_revisions": nulled,
                  "cascaded": cascaded}
        if erased or cascaded:
            result["not_guaranteed"] = list(NOT_GUARANTEED)
            result["artifacts"] = predating_artifacts(db, now)
        return result
