"""Lifecycle audit events (spec 5, 6): expire, delete, backup, restore, export and maintenance write
content-free events; the chain verifies; editing or deleting any row makes verification fail."""
from __future__ import annotations

import unittest

from recall_v2.store import audit

from .governance_helpers import GovernanceCase

CANARY = "zqxauditcanary5150"


class LifecycleAuditTests(GovernanceCase):
    def setUp(self):
        super().setUp()
        note = self.ok(self.alice.save(kind="note", request_id="a", key=f"k-{CANARY}", title=CANARY, body=CANARY))
        other = self.ok(self.alice.save(kind="note", request_id="b", key="other", body=f"other {CANARY}"))
        self.ok(self.alice.expire(item_id=other["id"], request_id="c"))
        made = self.ok(self.op.backup())
        self.ok(self.op.delete(note["id"]))
        self.ok(self.alice.forget(item_id=other["id"], request_id="d"))
        self.ok(self.op.restore(made["path"], to=str(self.private_dir() / "r.sqlite3")))
        self.ok(self.op.export(principal="alice"))
        self.clock.now += 40 * 86_400 * 1_000_000
        self.ok(self.op.maintain())
        self.db = self.store.db
        self.rows = self.db.execute("SELECT * FROM audit ORDER BY id").fetchall()

    def test_events_present_and_content_free(self):
        actions = [row["action"] for row in self.rows]
        for action in ("expire", "delete", "backup", "restore", "export", "backup_prune", "export_prune",
                       "retention_prune"):
            self.assertIn(action, actions)
        self.assertEqual(set(actions) - audit.ACTIONS, set())
        for row in self.rows:
            for value in tuple(row):
                if isinstance(value, str):
                    self.assertNotIn(CANARY, value)
                    self.assertNotIn("/", value)          # no paths either
        self.assertTrue(self.store.verify_audit()["ok"])
        self.assertTrue(self.ok(self.op.audit_verify())["ok"])

    def test_editing_one_row_fails_verify(self):
        self.db.execute("DROP TRIGGER audit_append_only")   # someone with file access
        for column, value in (("actor", "mallory"), ("action", "save"), ("at", 1), ("item_id", "X")):
            target = self.rows[len(self.rows) // 2]["id"]
            with self.subTest(column=column):
                self.db.execute("SAVEPOINT t")
                self.db.execute(f"UPDATE audit SET {column} = ? WHERE id = ?", (value, target))
                result = audit.verify_chain(self.db)
                self.db.execute("ROLLBACK TO t")
                self.db.execute("RELEASE t")
                self.assertFalse(result["ok"])
                self.assertEqual(result["first_bad_id"], target)
        self.assertTrue(audit.verify_chain(self.db)["ok"])

    def test_deleting_an_audit_row_fails_verify(self):
        for target in (self.rows[0]["id"], self.rows[len(self.rows) // 2]["id"]):
            with self.subTest(row=target):
                self.db.execute("SAVEPOINT t")
                self.db.execute("DELETE FROM audit WHERE id = ?", (target,))
                result = audit.verify_chain(self.db)
                self.db.execute("ROLLBACK TO t")
                self.db.execute("RELEASE t")
                self.assertFalse(result["ok"])
                self.assertEqual(result["first_bad_id"], target + 1)
        self.assertTrue(audit.verify_chain(self.db)["ok"])
        # committed deletion: verify and doctor both report the broken chain
        self.db.execute("DELETE FROM audit WHERE id = ?", (self.rows[3]["id"],))
        self.assertFalse(self.ok(self.op.audit_verify())["ok"])
        doctor = self.ok(self.op.doctor())
        self.assertFalse(doctor["audit"]["ok"])
        self.assertEqual(doctor["audit"]["first_bad_id"], self.rows[4]["id"])
        self.assertFalse(doctor["ok"])


if __name__ == "__main__":
    unittest.main()
