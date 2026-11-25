"""Every write and review action appends one content-free audit event; the chain verifies (spec 5)."""
from __future__ import annotations

import unittest

from .governance_helpers import GovernanceCase

SECRET = "zz-canary-content-7f3a"


class GovernanceAuditTests(GovernanceCase):
    def actions_since(self, start: int) -> list[tuple]:
        return [tuple(r) for r in self.store.db.execute(
            "SELECT actor, action, rev, project_id FROM audit WHERE id > ? ORDER BY id", (start,))]

    def last_id(self) -> int:
        return self.store.db.execute("SELECT COALESCE(MAX(id), 0) FROM audit").fetchone()[0]

    def test_sequence_of_operations(self):
        start = self.last_id()
        note = self.ok(self.alice.save(kind="note", request_id="a1", key="k", title=SECRET, body=SECRET))
        self.alice.save(kind="note", request_id="a1", key="k", title=SECRET, body=SECRET)        # replay
        self.alice.save(kind="note", request_id="a2", key="k", title=SECRET, body=SECRET)        # exact dup
        self.alice.save(kind="note", request_id="a1", key="k", body="other")                    # rejected
        cand = self.project_note(self.bob, "plan", SECRET, request_id="b1")
        self.ok(self.op.review_edit(cand["id"], 1, body=SECRET + " edited"))
        other = self.project_note(self.bob, "other", SECRET, request_id="b2")
        self.ok(self.op.review_reject(other["id"], 1))
        fact = self.project_fact(self.alice, SECRET, valid_from="2026-01-01T00:00:00.000000Z", request_id="f1")
        self.ok(self.op.review_approve(fact["id"], 1))
        self.project_fact(self.bob, "different", valid_from="2025-12-01T00:00:00.000000Z",
                          valid_to="2026-03-01T00:00:00.000000Z", request_id="f2")
        [conflict] = self.results(self.op.conflict_list(status="open"))
        self.ok(self.op.conflict_resolve(conflict["conflict_id"], "supersede"))
        self.ok(self.bob.retract(item_id=fact["id"], rev=2, request_id="r1"))
        self.ok(self.alice.pin(item_id=note["id"]))

        self.assertEqual(self.actions_since(start), [
            ("alice", "save", 1, None),
            ("bob", "save", 1, "proj"),
            ("~operator", "review_edit", 2, "proj"),
            ("~operator", "review_reject", 1, "proj"),
            ("~operator", "review_approve", 2, "proj"),
            ("bob", "save", 1, "proj"),
            ("~operator", "review_reject", 1, "proj"),
            ("alice", "save", 1, "proj"),
            ("~operator", "review_approve", 1, "proj"),
            ("bob", "save", 2, "proj"),
            ("~operator", "review_approve", 2, "proj"),
            ("~operator", "conflict_resolve", 2, "proj"),
            ("bob", "retract", 3, "proj"),
            ("alice", "pin", None, None),
        ])
        # Content-free: no column of any audit row holds stored text.
        for row in self.store.db.execute("SELECT * FROM audit"):
            for value in tuple(row):
                if isinstance(value, str):
                    self.assertNotIn("canary", value)
                    self.assertNotIn("different", value)
        self.assertEqual(self.ok(self.op.audit_verify()), {"ok": True, "checked": self.last_id(), "first_bad_id": None})

    def test_tampering_is_detected(self):
        self.ok(self.alice.save(kind="note", request_id="a1", key="k", body="b"))
        self.store.db.execute("DROP TRIGGER audit_append_only")
        self.store.db.execute("UPDATE audit SET actor = 'mallory' WHERE id = (SELECT MAX(id) FROM audit)")
        self.assertFalse(self.ok(self.op.audit_verify())["ok"])


if __name__ == "__main__":
    unittest.main()
