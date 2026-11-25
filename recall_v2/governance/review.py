"""The review queue (section 3.6): list candidates, approve, reject, and edit-then-approve.

All functions here are operator functions: the first argument must be an OperatorSession. Each action
runs in one IMMEDIATE transaction; approval is a compare-and-swap on the candidate's base revision and
re-checks the author's membership and revocation (stale_review otherwise).
"""
from __future__ import annotations

import difflib
import json
from typing import Optional

from ..core import model, normalise, timefmt, validity
from ..core.errors import RecallError, invalid
from ..retrieval.candidates import COLUMNS, enrich
from ..store import audit
from ..store.connection import read_transaction, write_transaction
from ..store.repository import REVISION_FROM
from .authority import OperatorSession, require_operator

# Candidates are never auto-deleted; after this age they are listed by `review list --stale`.
STALE_AFTER_MICROS = 30 * timefmt.MICROS_PER_DAY
UNSET = object()


def _check_rev(rev) -> None:
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise invalid("rev must be an integer.")


def operator_item(db, item_id: str, rev: int) -> dict:
    """The ITEM of one revision as the operator sees it (the operator reads everything)."""
    row = db.execute(f"SELECT {COLUMNS} FROM {REVISION_FROM} WHERE i.id = ? AND r.rev = ?", (item_id, rev)).fetchone()
    if row is None:
        raise RecallError("not_found", "No such revision.")
    return enrich(db, model.OPERATOR_ACTOR, row)


def _render(row) -> list[str]:
    """Text form of one revision for the review diff."""
    if row is None:
        return []
    interval = f"valid [{timefmt.format_rfc3339(row['valid_from'])}, " \
               f"{'open' if row['valid_to'] is None else timefmt.format_rfc3339(row['valid_to'])})"
    if row["kind"] == "fact":
        return [f"{row['subject']} | {row['predicate']} = {row['value_json']} ({row['op']})", interval]
    lines = [f"title: {row['title'] or ''}"]
    lines.extend((row["body"] or "").split("\n"))
    lines.append(interval)
    return lines


# -- listing -----------------------------------------------------------------------------------------

def list_candidates(session: OperatorSession, *, stale: bool = False) -> list[dict]:
    """Every open candidate with scope, source, kind, age, diff against current, flags and open conflicts.

    With stale=True, only candidates older than 30 days. Listing never deletes or changes anything.
    """
    store = require_operator(session).store
    if not isinstance(stale, bool):
        raise invalid("stale must be a boolean.")
    with read_transaction(store.db) as db:
        now = store.clock()
        rows = db.execute(
            f"SELECT {COLUMNS} FROM {REVISION_FROM} WHERE r.state = 'candidate' AND i.deleted_at IS NULL"
            " AND (:stale = 0 OR r.recorded_at < :cutoff) ORDER BY r.recorded_at, i.id, r.rev",
            {"stale": int(stale), "cutoff": now - STALE_AFTER_MICROS}).fetchall()
        out = []
        for row in rows:
            current = None
            if row["current_rev"]:
                current = db.execute(f"SELECT {COLUMNS} FROM {REVISION_FROM} WHERE i.id = ? AND r.rev = ?",
                                     (row["item_id"], row["current_rev"])).fetchone()
            item = enrich(db, model.OPERATOR_ACTOR, row)
            meta = json.loads(row["meta_json"] or "{}")
            out.append({
                "item_id": row["item_id"], "rev": row["rev"], "kind": row["kind"], "scope": item["scope"],
                "key": item["key"], "fact": item["fact"], "author": row["actor_id"],
                "recorded_at": item["recorded_at"], "age_seconds": (now - row["recorded_at"]) // 1_000_000,
                "stale": row["recorded_at"] < now - STALE_AFTER_MICROS,
                "base_rev": meta.get("base_rev"), "current_rev": row["current_rev"],
                "source_ref": meta.get("source_ref"), "sources": item["sources"],
                "flags": [{"type": f["type"], "other_item_id": f["other_item_id"]} for f in db.execute(
                    "SELECT type, other_item_id FROM flags WHERE item_id = ? AND rev = ? ORDER BY type, other_item_id",
                    (row["item_id"], row["rev"]))],
                "conflicts": [c[0] for c in db.execute(
                    "SELECT id FROM conflicts WHERE item_id = ? AND rev_b = ? AND status = 'open' ORDER BY id",
                    (row["item_id"], row["rev"]))],
                "diff": list(difflib.unified_diff(
                    _render(current), _render(row),
                    f"current r{row['current_rev']}" if current is not None else "current (none)",
                    f"candidate r{row['rev']}", lineterm="")),
            })
        return out


# -- approve and reject ------------------------------------------------------------------------------

def approve(session: OperatorSession, item_id: str, rev: int) -> dict:
    store = require_operator(session).store
    _check_rev(rev)
    with write_transaction(store.db) as db:
        now = store.clock()
        store._review_in(db, now, item_id, rev, "accepted", model.OPERATOR_ACTOR, None)
        return operator_item(db, item_id, rev)


def reject(session: OperatorSession, item_id: str, rev: int, *, note: Optional[str] = None) -> dict:
    store = require_operator(session).store
    _check_rev(rev)
    if note is not None and (not isinstance(note, str) or len(note.encode("utf-8")) > model.TITLE_MAX_BYTES):
        raise invalid("note must be text of at most 256 bytes.")
    with write_transaction(store.db) as db:
        now = store.clock()
        store._review_in(db, now, item_id, rev, "rejected", model.OPERATOR_ACTOR, note)
        return operator_item(db, item_id, rev)


# -- edit then approve -------------------------------------------------------------------------------

def edit(session: OperatorSession, item_id: str, rev: int, *, title=UNSET, body=UNSET, value=UNSET,
         valid_from=UNSET, valid_to=UNSET) -> dict:
    """Write a new candidate revision with the operator as actor (the editor), then approve it.

    The edited-from candidate is rejected with review_note "edited". Fields not given are copied from it.
    Fails with stale_review if the approval basis of the edited-from candidate has changed.
    """
    store = require_operator(session).store
    _check_rev(rev)
    with write_transaction(store.db) as db:
        now = store.clock()
        new_rev = edit_in(store, db, now, item_id, rev, title=title, body=body, value=value,
                          valid_from=valid_from, valid_to=valid_to)
        return operator_item(db, item_id, new_rev)


def edit_in(store, db, now: int, item_id: str, rev: int, *, title=UNSET, body=UNSET, value=UNSET,
            valid_from=UNSET, valid_to=UNSET) -> int:
    """The body of `edit`, inside the caller's IMMEDIATE transaction. Returns the new accepted rev."""
    row = store.review_row(db, item_id, rev)
    if row["state"] != "candidate":
        raise invalid("Only candidates can be edited.")
    store.check_approval_basis(db, row)
    old = db.execute(f"SELECT {COLUMNS} FROM {REVISION_FROM} WHERE i.id = ? AND r.rev = ?", (item_id, rev)).fetchone()
    meta = json.loads(old["meta_json"] or "{}")
    base = meta.get("base_rev", old["current_rev"])

    start = old["valid_from"] if valid_from is UNSET else timefmt.coerce(valid_from)
    if start is None:
        raise invalid("valid_from cannot be empty.")
    history = store._fact_history(db, item_id) if old["kind"] == "fact" else []
    revisions = [h for h, _ in history]
    if valid_to is not UNSET:
        end = timefmt.coerce(valid_to)
    elif valid_from is not UNSET and old["kind"] == "fact" and old["op"] == validity.ASSERT:
        end = validity.default_valid_to(start, revisions)
    else:
        end = old["valid_to"]
    validity.check_interval(start, end)

    scope = old["scope_kind"]
    project = old["project_id"]
    if old["kind"] == "fact":
        if title is not UNSET or body is not UNSET:
            raise invalid("A fact is edited by value and interval only.")
        if old["op"] == validity.RETRACT and value is not UNSET:
            raise invalid("A retraction has no value to edit.")
        new_value = json.loads(old["value_json"]) if value is UNSET else value
        req = model.validate_save(kind="fact", scope=scope, project=project,
                                  fact={"subject": old["subject"], "predicate": old["predicate"], "value": new_value},
                                  valid_from=start, valid_to=end)
        new_title, new_body, value_json = None, None, req.fact.value_json
    else:
        if value is not UNSET:
            raise invalid("value is only for facts.")
        req = model.validate_save(kind=old["kind"], scope=scope, project=project, key=old["key"],
                                  title=old["title"] if title is UNSET else title,
                                  body=old["body"] if body is UNSET else body,
                                  valid_from=start, valid_to=end, expected_revision=base,
                                  source_ref=meta.get("source_ref"))
        new_title, new_body, value_json = req.title, req.body, None

    new_rev = db.execute("SELECT COALESCE(MAX(rev), 0) + 1 FROM item_revisions WHERE item_id = ?",
                         (item_id,)).fetchone()[0]
    if new_rev > model.MAX_REV:
        raise invalid("The item has too many revisions.")
    new_meta = {"base_rev": base, "edited_from": rev}
    if meta.get("source_ref") is not None:
        new_meta["source_ref"] = meta["source_ref"]
    db.execute(
        "INSERT INTO item_revisions (item_id, rev, state, title, body, body_hash, meta_json, valid_from, valid_to,"
        " actor_id, request_id, recorded_at, accepted_at) VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
        (item_id, new_rev, new_title, new_body, None if new_body is None else normalise.body_hash(new_body),
         normalise.canonical_json(new_meta), start, end, model.OPERATOR_ACTOR, now))
    if old["kind"] == "fact":
        if old["op"] == validity.ASSERT:
            superseded = validity.current_fact(revisions, start, now)
            supersedes = superseded.rev if superseded else None
        else:
            supersedes = old["supersedes_rev"]
        db.execute(
            "INSERT INTO fact_versions (item_id, rev, op, subject, predicate, value_json, supersedes_rev)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, new_rev, old["op"], old["subject"], old["predicate"], value_json, supersedes))
        if old["op"] == validity.ASSERT:
            # Conflicts of the edited value are recorded, then closed by the approval below.
            for existing, existing_value in history:
                if validity.conflicts_with(start, end, value_json, existing, existing_value, revisions):
                    db.execute("INSERT INTO conflicts (id, item_id, rev_a, rev_b, status) VALUES (?, ?, ?, ?, 'open')",
                               (store._ulid(now), item_id, existing.rev, new_rev))
    audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="review_edit", item_id=item_id, rev=new_rev,
                 project_id=project)
    store._review_in(db, now, item_id, rev, "rejected", model.OPERATOR_ACTOR, "edited")
    store._review_in(db, now, item_id, new_rev, "accepted", model.OPERATOR_ACTOR, None)
    return new_rev
