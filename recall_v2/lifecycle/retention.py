"""Retention and maintenance (section 6, `recall maintain`). Nothing runs on a timer.

Defaults:
- notes, facts, briefs, excerpts: kept until deleted;
- checkpoints: the current revision plus 90 days of revisions; older non-current revisions have their
  content nulled (the revision row, its interval and state remain);
- candidates: never removed (listed by `review list --stale` after 30 days);
- rejected revisions: content nulled 90 days after review;
- receipts: removed after 30 days;
- audit: rows older than 365 days removed from the start of the chain; the last removed hash becomes the
  anchor that verification starts from;
- backups and exports: files and registrations removed 30 days after creation;
- extraction manifests live with their runs: a manifest that no run used is removed 30 days after it
  was planned.

Then the storage pass: FTS optimize, VACUUM and WAL truncation (store.maintenance.compact).

Idempotent: a second run at the same time changes nothing. One content-free `retention_prune` audit
event is written only when the run changed something.
"""
from __future__ import annotations

from ..core import model, timefmt
from ..governance.authority import require_operator
from ..store import audit, erasure, maintenance as storage
from ..store.connection import write_transaction
from .backup import prune_in

DAY = timefmt.MICROS_PER_DAY
CHECKPOINT_DAYS = 90
REJECTED_DAYS = 90
RECEIPT_DAYS = 30
AUDIT_DAYS = 365
ORPHAN_MANIFEST_DAYS = 30


def _null_revisions(db, rows) -> int:
    count = 0
    for row in rows:
        db.execute("DELETE FROM fts_items WHERE rowid IN (SELECT rowid FROM item_revisions WHERE item_id = ? AND rev = ?)",
                   (row[0], row[1]))
        if erasure.null_revision(db, row[0], row[1]):
            count += 1
    return count


def _prune_audit(db, cutoff: int) -> int:
    first_kept = db.execute("SELECT MIN(id) FROM audit WHERE at >= ?", (cutoff,)).fetchone()[0]
    if first_kept is None:
        last = db.execute("SELECT id, hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    else:
        last = db.execute("SELECT id, hash FROM audit WHERE id < ? ORDER BY id DESC LIMIT 1", (first_kept,)).fetchone()
    if last is None:
        return 0
    removed = db.execute("DELETE FROM audit WHERE id <= ?", (last["id"],)).rowcount
    db.execute("INSERT INTO meta (name, value) VALUES (?, ?) ON CONFLICT (name) DO UPDATE SET value = excluded.value",
               (audit.ANCHOR_NAME, last["hash"].hex()))
    return removed


def maintenance(session) -> dict:
    store = require_operator(session).store
    with write_transaction(store.db) as db:
        now = store.clock()
        checkpoints = _null_revisions(db, db.execute(
            "SELECT r.item_id, r.rev FROM item_revisions AS r JOIN items AS i ON i.id = r.item_id"
            " WHERE i.kind = 'checkpoint' AND i.deleted_at IS NULL AND r.rev <> i.current_rev"
            " AND r.state <> 'candidate' AND r.recorded_at < ?"
            " AND (r.title IS NOT NULL OR r.body IS NOT NULL OR r.body_hash IS NOT NULL OR r.meta_json IS NOT NULL)"
            " ORDER BY r.item_id, r.rev", (now - CHECKPOINT_DAYS * DAY,)).fetchall())
        rejected = _null_revisions(db, db.execute(
            "SELECT r.item_id, r.rev FROM item_revisions AS r LEFT JOIN fact_versions AS f USING (item_id, rev)"
            " WHERE r.state = 'rejected' AND COALESCE(r.reviewed_at, r.recorded_at) < ?"
            " AND (r.title IS NOT NULL OR r.body IS NOT NULL OR r.body_hash IS NOT NULL OR r.meta_json IS NOT NULL"
            " OR f.subject IS NOT NULL OR f.predicate IS NOT NULL OR f.value_json IS NOT NULL)"
            " ORDER BY r.item_id, r.rev", (now - REJECTED_DAYS * DAY,)).fetchall())
        receipts = db.execute("DELETE FROM receipts WHERE recorded_at < ?", (now - RECEIPT_DAYS * DAY,)).rowcount
        manifests = db.execute(
            "DELETE FROM extraction_manifests WHERE planned_at < ? AND NOT EXISTS (SELECT 1 FROM extraction_runs AS x"
            " WHERE x.manifest_hash = extraction_manifests.manifest_hash)", (now - ORPHAN_MANIFEST_DAYS * DAY,)).rowcount
        artifacts = prune_in(db, now, ("backup", "export"))
        audit_rows = _prune_audit(db, now - AUDIT_DAYS * DAY)
        result = {"checkpoint_revisions_pruned": checkpoints, "rejected_revisions_pruned": rejected,
                  "receipts_removed": receipts, "manifests_removed": manifests,
                  "artifacts_pruned": [a["path"] for a in artifacts], "audit_rows_pruned": audit_rows}
        if checkpoints or rejected or receipts or manifests or audit_rows:
            audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="retention_prune")
    storage.compact(store.db)
    result["compacted"] = True
    return result
