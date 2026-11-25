"""doctor (spec 5, 7.5): a read-only, JSON-serialisable report with a stable key order."""
from __future__ import annotations

from hashlib import sha256
import json
import unittest

from recall_v2.core import timefmt
from recall_v2.core.errors import RecallError
from recall_v2.lifecycle import doctor as doctor_module
from recall_v2.store.schema import USER_VERSION

from .governance_helpers import GovernanceCase

KEYS = ["ok", "checked_at", "schema", "sqlite_version", "integrity_check", "foreign_key_violations", "audit",
        "candidates", "conflicts", "items", "erasures", "artifacts"]


class DoctorTests(GovernanceCase):
    def setUp(self):
        super().setUp()
        self.project_note(self.alice, "old", "old candidate")
        self.clock.now += 31 * timefmt.MICROS_PER_DAY
        self.project_note(self.bob, "new", "new candidate")
        accepted = self.project_fact(self.alice, "blue")
        self.ok(self.op.review_approve(accepted["id"], accepted["rev"]))
        self.project_fact(self.bob, "red", valid_from=self.revision(accepted["id"], 1)["valid_from"])  # open conflict
        gone = self.ok(self.alice.save(kind="note", request_id="g", key="gone", body="x"))
        self.ok(self.op.delete(gone["id"]))
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def file_hashes(self):
        out = {}
        for path in sorted(self.path.parent.iterdir()):
            if path.is_file():
                out[path.name] = sha256(path.read_bytes()).hexdigest()
        return out

    def test_report_contents_and_key_order(self):
        report = self.ok(self.op.doctor())
        self.assertEqual(list(report), KEYS)
        self.assertTrue(report["ok"])
        self.assertEqual(report["schema"]["user_version"], USER_VERSION)
        self.assertEqual(report["schema"]["envelope"], "recall.v2")
        self.assertEqual(report["integrity_check"], "ok")
        self.assertEqual(report["foreign_key_violations"], 0)
        self.assertTrue(report["audit"]["ok"])
        self.assertGreater(report["audit"]["checked"], 5)
        self.assertEqual(report["candidates"]["open"], 3)
        self.assertEqual(report["candidates"]["stale"], 1)
        self.assertEqual(report["conflicts"]["open"], 1)
        self.assertEqual(len(report["conflicts"]["items"]), 1)
        self.assertEqual(report["items"]["deleted"], 1)
        self.assertEqual(report["erasures"], 1)
        text = json.dumps(report)
        self.assertEqual(list(json.loads(text)), KEYS)
        again = self.ok(self.op.doctor())
        again.pop("checked_at")
        report.pop("checked_at")
        self.assertEqual(json.dumps(again), json.dumps(report))     # same state, same bytes

    def test_doctor_is_read_only(self):
        before = self.file_hashes()
        audit_rows = self.count("audit")
        for _ in range(3):
            self.ok(self.op.doctor())
        self.assertEqual(self.file_hashes(), before)
        self.assertEqual(self.count("audit"), audit_rows)
        self.assertFalse(self.store.db.in_transaction)

    def test_operator_only(self):
        for target in (self.alice, self.store, None):
            with self.assertRaises(RecallError) as raised:
                doctor_module.doctor(target)
            self.assertEqual(raised.exception.code, "forbidden")
        self.assertFalse(hasattr(self.alice, "doctor"))


if __name__ == "__main__":
    unittest.main()
