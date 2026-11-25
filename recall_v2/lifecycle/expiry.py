"""Expiry (section 6): a new revision ends an item's validity; expired items leave default reads, appear
in `expire_check`, and are never deleted.

Non-fact kinds. The new revision copies the current accepted revision's content and interval start and
sets valid_to = `at`. The current revision of a non-fact item is the latest accepted one, returned only
while its interval contains the read time (2.5), so from `at` on the item is expired. Earlier read times
(as_of) and history still show it.

Facts. A fact's current value is chosen among every accepted revision covering the read time (2.5), so a
copy with an earlier valid_to would let the older open-ended revision win again from `at` on. A fact is
therefore expired by a `retract` revision covering [at, end of the expired value's effective interval),
marked `{"expiry": true}` in its meta: from `at` on the key has no current value, before `at` nothing
changes, and a later-starting value is never hidden (the retraction ends where it begins).

Boundary: intervals are closed-open, so an item whose valid_to equals `now` is expired at `now`.

Writers: a principal may expire items in its private scope (accepted at once) and, as a candidate for
review, items in a project where it is a member. The operator (policy path) writes accepted revisions.
"""
from __future__ import annotations

import json
from typing import Optional

from ..core import model, normalise, timefmt, validity
from ..core.errors import RecallError, invalid
from ..governance.authority import require_operator
from ..governance.review import operator_item
from ..retrieval.candidates import COLUMNS
from ..store import audit, receipts
from ..store.connection import read_transaction, write_transaction
from ..store.repository import CURRENT_OTHER_REV, REVISION_FROM, Principal
from ..store.visibility import visible_items, visible_revisions

EXPIRY_MARK = "expiry"


def _next_rev(db, item_id: str) -> int:
    rev = db.execute("SELECT COALESCE(MAX(rev), 0) + 1 FROM item_revisions WHERE item_id = ?", (item_id,)).fetchone()[0]
    if rev > model.MAX_REV:
        raise invalid("The item has too many revisions.")
    return rev


def expire_in(store, db, now: int, item_id: str, at: Optional[int], actor: str, state: str) -> dict:
    """Write the expiring revision inside the caller's transaction. Returns {"item_id", "rev"}."""
    item = db.execute("SELECT * FROM items WHERE id = ? AND deleted_at IS NULL", (item_id,)).fetchone()
    if item is None:
        raise RecallError("not_found", "No such item.")
    at = now if at is None else at
    rev = _next_rev(db, item_id)
    accepted_at = now if state == "accepted" else None
    meta = {"base_rev": item["current_rev"], EXPIRY_MARK: True}

    if item["kind"] == "fact":
        history = store._fact_history(db, item_id)
        revisions = [h for h, _ in history]
        target = validity.current_fact(revisions, at, now)
        if target is None:
            raise invalid("The fact has no current value at that time; nothing to expire.")
        _, end = validity.effective_interval(target, revisions)
        validity.check_interval(at, end)
        source = db.execute("SELECT subject, predicate, value_json FROM fact_versions WHERE item_id = ? AND rev = ?",
                            (item_id, target.rev)).fetchone()
        db.execute(
            "INSERT INTO item_revisions (item_id, rev, state, meta_json, valid_from, valid_to, actor_id, request_id,"
            " recorded_at, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (item_id, rev, state, normalise.canonical_json(meta), at, end, actor, now, accepted_at))
        db.execute(
            "INSERT INTO fact_versions (item_id, rev, op, subject, predicate, value_json, supersedes_rev)"
            " VALUES (?, ?, 'retract', ?, ?, ?, ?)",
            (item_id, rev, source["subject"], source["predicate"], source["value_json"], target.rev))
    else:
        current = db.execute(
            "SELECT * FROM item_revisions WHERE item_id = ? AND state = 'accepted' ORDER BY rev DESC LIMIT 1",
            (item_id,)).fetchone()
        if current is None:
            raise invalid("The item has no accepted revision to expire.")
        if current["valid_to"] is not None and current["valid_to"] <= at:
            raise invalid("The item already ends at or before that time.")
        validity.check_interval(current["valid_from"], at)
        old_meta = json.loads(current["meta_json"] or "{}")
        if old_meta.get("source_ref") is not None:
            meta["source_ref"] = old_meta["source_ref"]
        db.execute(
            "INSERT INTO item_revisions (item_id, rev, state, title, body, body_hash, meta_json, valid_from, valid_to,"
            " actor_id, request_id, recorded_at, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (item_id, rev, state, current["title"], current["body"], current["body_hash"],
             normalise.canonical_json(meta), current["valid_from"], at, actor, now, accepted_at))
    if state == "accepted":
        db.execute("UPDATE items SET current_rev = MAX(current_rev, ?) WHERE id = ?", (rev, item_id))
        store._fts_index(db, item_id, rev, item["kind"])
    audit.append(db, at=now, actor=actor, action="expire", item_id=item_id, rev=rev, project_id=item["project_id"])
    return {"item_id": item_id, "rev": rev}


def expire_item(session, item_id: str, at=None) -> dict:
    """Operator (policy) expiry: an accepted expiring revision. Returns its ITEM."""
    store = require_operator(session).store
    at = timefmt.coerce(at)
    with write_transaction(store.db) as db:
        result = expire_in(store, db, store.clock(), item_id, at, model.OPERATOR_ACTOR, "accepted")
        return operator_item(db, result["item_id"], result["rev"])


def expire_as(store, principal: Principal, *, item_id: str, request_id: str, at=None) -> dict:
    """A principal's expiry of a visible item: accepted in its private scope, a candidate in a project."""
    normalise.check_id(request_id, normalise.REQUEST_ID, "request_id")
    at = timefmt.coerce(at)
    digest = receipts.fingerprint({"op": "expire", "item_id": item_id, "at": at})
    with write_transaction(store.db) as db:
        now = store.clock()
        caller = store._caller(db, principal.token_digest)
        previous = receipts.replay(db, caller, request_id, digest, now)
        if previous is not None:
            return previous
        item = db.execute(
            f"SELECT scope_kind FROM items AS i WHERE i.id = :id AND {visible_items('i')} AND EXISTS ("
            f"SELECT 1 FROM item_revisions AS r WHERE r.item_id = i.id AND {visible_revisions('r', 'i')})",
            {"id": item_id, "caller": caller}).fetchone()
        if item is None:
            raise RecallError("not_found", "No such item.")
        result = expire_in(store, db, now, item_id, at, caller, model.state_on_write(item["scope_kind"]))
        receipts.record(db, caller, request_id, digest, result["item_id"], result["rev"], now)
        return result


def _fact_expiry(store, db, item_id: str, now: int) -> Optional[tuple[int, int]]:
    """(rev, expired_at) if the fact has no current value at now because its validity ended."""
    history = store._fact_history(db, item_id)
    revisions = [h for h, _ in history]
    if validity.current_fact(revisions, now, now) is not None:
        return None
    covering = [r for r in revisions if r.state == "accepted" and r.accepted_at is not None
                and r.accepted_at <= now and validity.contains(r.valid_from, r.valid_to, now)]
    if covering:
        winner = max(covering, key=lambda r: (r.accepted_at, r.rev))
        meta = db.execute("SELECT meta_json FROM item_revisions WHERE item_id = ? AND rev = ?",
                          (item_id, winner.rev)).fetchone()[0]
        if json.loads(meta or "{}").get(EXPIRY_MARK):
            return winner.rev, winner.valid_from
        return None   # retracted, not expired
    started = [r for r in revisions if r.state == "accepted" and r.op == validity.ASSERT
               and r.accepted_at is not None and r.accepted_at <= now and r.valid_from <= now]
    if started and all(r.valid_to is not None and r.valid_to <= now for r in started):
        last = max(started, key=lambda r: (r.valid_to, r.rev))
        return last.rev, last.valid_to
    return None


def expire_check(session) -> list[dict]:
    """Every live item expired at now: kind, scope, the revision whose validity ended, and when.
    Ordered by expired_at, then item ID. Lists only; never changes or deletes anything."""
    store = require_operator(session).store
    with read_transaction(store.db) as db:
        now = store.clock()
        out = []
        for row in db.execute(
                f"SELECT {COLUMNS} FROM {REVISION_FROM} WHERE i.deleted_at IS NULL AND i.kind <> 'fact'"
                f" AND r.rev = {CURRENT_OTHER_REV} AND r.valid_to IS NOT NULL AND r.valid_to <= :tk",
                {"tk": now}).fetchall():
            out.append((row["valid_to"], row["item_id"], row["kind"], row["scope_kind"], row["owner_id"],
                        row["project_id"], row["rev"]))
        for row in db.execute("SELECT id, scope_kind, owner_id, project_id FROM items"
                              " WHERE kind = 'fact' AND deleted_at IS NULL AND current_rev > 0").fetchall():
            found = _fact_expiry(store, db, row["id"], now)
            if found is not None:
                out.append((found[1], row["id"], "fact", row["scope_kind"], row["owner_id"], row["project_id"],
                            found[0]))
        out.sort(key=lambda e: (e[0], e[1]))
        return [{"item_id": item_id, "kind": kind,
                 "scope": {"type": scope, "id": owner if scope == "private" else project},
                 "rev": rev, "expired_at": timefmt.format_rfc3339(at)}
                for at, item_id, kind, scope, owner, project, rev in out]
