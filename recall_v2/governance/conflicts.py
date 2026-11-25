"""Conflict listing and resolution (section 3.4). Operator functions only.

A conflict row pairs an accepted assert (rev_a) with a later-written revision (rev_b) of the same fact
key that disagrees about an overlapping period. Open conflicts are closed only by an explicit operator
action: `supersede` (approve rev_b), `keep_both` (rev_b gets, or already has, a valid interval disjoint
from every accepted assert of a different value, then is approved) or `reject` (reject rev_b).
"""
from __future__ import annotations

from typing import Optional

from ..core import model, timefmt, validity
from ..core.errors import RecallError, invalid
from ..store.connection import read_transaction, write_transaction
from .authority import OperatorSession, require_operator
from .review import UNSET, edit_in, operator_item

ACTIONS = ("supersede", "keep_both", "reject")
STATUSES = ("open", "auto_resolved", "resolved")


def _ts(value: Optional[int]) -> Optional[str]:
    return None if value is None else timefmt.format_rfc3339(value)


def list_conflicts(session: OperatorSession, *, status: Optional[str] = None) -> list[dict]:
    store = require_operator(session).store
    if status is not None and status not in STATUSES:
        raise invalid("status must be open, auto_resolved or resolved.")
    with read_transaction(store.db) as db:
        rows = db.execute(
            "SELECT c.*, i.scope_kind, i.owner_id, i.project_id FROM conflicts AS c JOIN items AS i ON i.id = c.item_id"
            " WHERE i.deleted_at IS NULL AND (:status IS NULL OR c.status = :status) ORDER BY c.id",
            {"status": status}).fetchall()
        return [{"conflict_id": r["id"], "item_id": r["item_id"], "rev_a": r["rev_a"], "rev_b": r["rev_b"],
                 "status": r["status"], "resolved_by": r["resolved_by"], "resolved_at": _ts(r["resolved_at"]),
                 "scope": {"type": r["scope_kind"],
                           "id": r["owner_id"] if r["scope_kind"] == "private" else r["project_id"]}}
                for r in rows]


def resolve(session: OperatorSession, conflict_id: str, action: str, *, valid_from=UNSET, valid_to=UNSET) -> dict:
    """Resolve one open conflict. Returns the ITEM of the resulting revision."""
    store = require_operator(session).store
    if action not in ACTIONS:
        raise invalid("action must be supersede, keep_both or reject.")
    if action != "keep_both" and (valid_from is not UNSET or valid_to is not UNSET):
        raise invalid("Only keep_both takes a new interval.")
    with write_transaction(store.db) as db:
        now = store.clock()
        row = db.execute("SELECT c.*, i.deleted_at FROM conflicts AS c JOIN items AS i ON i.id = c.item_id"
                         " WHERE c.id = ?", (conflict_id,)).fetchone()
        if row is None or row["deleted_at"] is not None:
            raise RecallError("not_found", "No such conflict.")
        if row["status"] == "auto_resolved":
            raise invalid("The conflict was resolved when it was written.")
        if row["status"] != "open":
            raise RecallError("stale_review", "The conflict is no longer open.")
        item_id, rev = row["item_id"], row["rev_b"]
        if action == "supersede":
            store._review_in(db, now, item_id, rev, "accepted", model.OPERATOR_ACTOR, None)
            result_rev = rev
        elif action == "reject":
            store._review_in(db, now, item_id, rev, "rejected", model.OPERATOR_ACTOR, "conflict_rejected")
            result_rev = rev
        else:
            result_rev = _keep_both(store, db, now, item_id, rev, valid_from, valid_to)
        item = operator_item(db, item_id, result_rev)
        return {"conflict_id": conflict_id, "action": action, "item": item}


def _keep_both(store, db, now: int, item_id: str, rev: int, valid_from, valid_to) -> int:
    candidate = db.execute(
        "SELECT r.valid_from, r.valid_to, f.op, f.value_json FROM item_revisions AS r"
        " JOIN fact_versions AS f ON f.item_id = r.item_id AND f.rev = r.rev WHERE r.item_id = ? AND r.rev = ?",
        (item_id, rev)).fetchone()
    start = candidate["valid_from"] if valid_from is UNSET else timefmt.coerce(valid_from)
    end = candidate["valid_to"] if valid_to is UNSET else timefmt.coerce(valid_to)
    if start is None:
        raise invalid("valid_from cannot be empty.")
    validity.check_interval(start, end)
    history = store._fact_history(db, item_id)
    revisions = [h for h, _ in history]
    for existing, value_json in history:
        if existing.state != "accepted" or existing.op != validity.ASSERT or value_json == candidate["value_json"]:
            continue
        e_from, e_to = validity.effective_interval(existing, revisions)
        if validity.overlaps(start, end, e_from, e_to):
            raise invalid("keep_both needs a valid interval disjoint from every accepted value it differs from.",
                          overlapping_rev=existing.rev)
    if (start, end) == (candidate["valid_from"], candidate["valid_to"]):
        store._review_in(db, now, item_id, rev, "accepted", model.OPERATOR_ACTOR, None)
        return rev
    return edit_in(store, db, now, item_id, rev, valid_from=start, valid_to=end)
