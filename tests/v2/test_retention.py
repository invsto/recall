"""Retention and maintenance (spec 6): defaults for checkpoints, candidates, rejected revisions, receipts,
audit, artifacts and extraction manifests, applied only by maintenance, which is idempotent."""
from __future__ import annotations

import unittest

from recall_v2.core import timefmt
from recall_v2.core.errors import RecallError
from recall_v2.lifecycle import retention
from recall_v2.store import audit

from .extraction_helpers import ExtractionCase

DAY = timefmt.MICROS_PER_DAY


def dump(db, *, physical_fts: bool = True) -> list:
    """Every table's rows (FTS shadow tables included unless physical_fts=False), as a comparable value."""
    out = []
    for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"):
        if not physical_fts and name in ("fts_items_data", "fts_items_idx"):
            continue
        out.append((name, sorted(tuple(r) for r in db.execute(f'SELECT * FROM "{name}"'))))
    return out


class RetentionTests(ExtractionCase):
    def build(self):
        """Checkpoint history, a rejected candidate, an old candidate, receipts, an orphan manifest, a backup."""
        rev = 0
        for n in range(3):
            self.ok(self.alice.save(kind="checkpoint", request_id=f"cp{n}", key="task:x",
                                    body=f'{{"step": {n}}}', expected_revision=rev))
            rev += 1
        self.rejected = self.project_note(self.bob, "bad", "rejected text")
        self.ok(self.op.review_reject(self.rejected["id"], 1))
        self.old_candidate = self.project_note(self.alice, "pending", "pending text")
        source = self.add_source(self.log())
        self.plan(source)                                     # never run: an orphan manifest
        self.backup = self.ok(self.op.backup())
        self.checkpoint_id = self.store.db.execute("SELECT id FROM items WHERE kind = 'checkpoint'").fetchone()[0]

    def test_defaults(self):
        self.build()
        audit_before = self.count("audit")
        # day 31: receipts, the orphan manifest and the backup go; content of history stays
        self.clock.now += 31 * DAY
        first = self.ok(self.op.maintain())
        self.assertGreater(first["receipts_removed"], 0)
        self.assertEqual(self.count("receipts"), 0)
        self.assertEqual(first["manifests_removed"], 1)
        self.assertEqual(first["artifacts_pruned"], [self.backup["path"]])
        self.assertEqual((first["checkpoint_revisions_pruned"], first["rejected_revisions_pruned"]), (0, 0))
        self.assertEqual(self.revision(self.checkpoint_id, 1)["body"], '{"step":0}')
        # day 91: old checkpoint revisions and the rejected revision lose their content; candidates stay
        self.clock.now += 60 * DAY
        second = self.ok(self.op.maintain())
        self.assertEqual(second["checkpoint_revisions_pruned"], 2)
        self.assertEqual(second["rejected_revisions_pruned"], 1)
        for rev in (1, 2):
            row = self.revision(self.checkpoint_id, rev)
            self.assertEqual((row["body"], row["body_hash"]), (None, None))
        self.assertEqual(self.revision(self.checkpoint_id, 3)["body"], '{"step":2}')     # current kept
        self.assertEqual(self.ok(self.alice.get(item_id=self.checkpoint_id))["body"], '{"step":2}')
        self.assertIsNone(self.revision(self.rejected["id"], 1)["body"])
        self.assertEqual(self.revision(self.old_candidate["id"], 1)["body"], "pending text")   # never auto-deleted
        self.assertEqual(self.revision(self.old_candidate["id"], 1)["state"], "candidate")
        self.assertTrue(self.ok(self.op.review_list(stale=True)))
        self.assertGreater(self.count("audit"), audit_before)
        # day 400: audit rows older than 365 days go; the anchor keeps the chain verifiable
        oldest = self.count("audit")
        self.clock.now += 310 * DAY
        third = self.ok(self.op.maintain())
        self.assertGreater(third["audit_rows_pruned"], 0)
        self.assertLess(self.count("audit"), oldest)
        self.assertTrue(self.store.verify_audit()["ok"])
        anchor = self.store.db.execute("SELECT value FROM meta WHERE name = ?", (audit.ANCHOR_NAME,)).fetchone()
        self.assertIsNotNone(anchor)
        self.assertTrue(self.ok(self.op.doctor())["audit"]["anchored"])

    def test_maintenance_is_idempotent(self):
        self.build()
        self.clock.step = 0
        for days in (31, 91, 400):
            self.clock.now += days * DAY
            self.ok(self.op.maintain())
            state = dump(self.store.db)
            again = self.ok(self.op.maintain())
            self.assertEqual(dump(self.store.db), state, days)
            self.assertEqual({k: v for k, v in again.items() if k != "compacted"},
                             {"checkpoint_revisions_pruned": 0, "rejected_revisions_pruned": 0, "receipts_removed": 0,
                              "manifests_removed": 0, "artifacts_pruned": [], "audit_rows_pruned": 0})
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_nothing_is_pruned_early(self):
        self.build()
        state = dump(self.store.db, physical_fts=False)    # optimize merges index segments: same content
        result = self.ok(self.op.maintain())
        self.assertEqual(result["receipts_removed"] + result["manifests_removed"] + result["audit_rows_pruned"], 0)
        self.assertEqual(dump(self.store.db, physical_fts=False), state)

    def test_operator_only(self):
        for target in (self.alice, self.store, None):
            with self.assertRaises(RecallError) as raised:
                retention.maintenance(target)
            self.assertEqual(raised.exception.code, "forbidden")
        self.assertFalse(hasattr(self.alice, "maintain"))


if __name__ == "__main__":
    unittest.main()
