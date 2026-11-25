"""Receipts through Client: replay, request_id_conflict and 30-day expiry (spec 3.5)."""
from __future__ import annotations

import unittest

from .governance_helpers import MICROS_PER_DAY, GovernanceCase

TABLES = ("items", "item_revisions", "fact_versions", "receipts", "audit", "conflicts", "flags")


class IdempotencyTests(GovernanceCase):
    def snapshot(self):
        return {table: self.count(table) for table in TABLES}

    def test_replay_returns_the_same_result_and_writes_nothing(self):
        for client, args in (
            (self.alice, dict(kind="note", request_id="r1", key="n", body="hello")),
            (self.alice, dict(kind="fact", request_id="r2", fact={"subject": "s", "predicate": "p", "value": 1})),
            (self.bob, dict(kind="note", request_id="r3", scope="project", project="proj", key="n", body="x")),
            (self.bob, dict(kind="fact", request_id="r4", scope="project", project="proj",
                            fact={"subject": "s", "predicate": "p", "value": 2})),
        ):
            first = client.save(**args)
            self.assertTrue(first["ok"], first)
            before = self.snapshot()
            self.assertEqual(client.save(**args), first)
            self.assertEqual(self.snapshot(), before)

        fact = self.ok(self.alice.lookup(subject="s"))
        first = self.alice.retract(item_id=fact["id"], rev=fact["rev"], request_id="r5")
        before = self.snapshot()
        self.assertEqual(self.alice.retract(item_id=fact["id"], rev=fact["rev"], request_id="r5"), first)
        self.assertEqual(self.snapshot(), before)

    def test_different_content_is_request_id_conflict_and_writes_nothing(self):
        self.ok(self.alice.save(kind="note", request_id="r1", key="n", body="hello"))
        before = self.snapshot()
        self.err(self.alice.save(kind="note", request_id="r1", key="n", body="changed"), "request_id_conflict")
        self.err(self.alice.save(kind="note", request_id="r1", key="n", body="hello", title="t"),
                 "request_id_conflict")
        self.err(self.alice.save(kind="note", request_id="r1", key="n", body="hello", expected_revision=1),
                 "request_id_conflict")
        self.assertEqual(self.snapshot(), before)
        # Receipts are per principal.
        self.ok(self.bob.save(kind="note", request_id="r1", key="n", body="changed"))

    def test_after_expiry_a_replay_is_a_new_request(self):
        args = dict(kind="note", request_id="r1", key="n", body="v1")
        first = self.ok(self.alice.save(**args))
        self.ok(self.alice.save(kind="note", request_id="r2", key="n", body="v2"))
        self.clock.now += 30 * MICROS_PER_DAY + 1
        # A new request: different content under the old request_id is no longer request_id_conflict ...
        replay = self.ok(self.alice.save(kind="note", request_id="r1", key="n", body="v1"))
        self.assertEqual(replay["rev"], 3)   # ... and the late replay writes again (no CAS given)
        self.assertNotEqual(replay["rev"], first["rev"])
        receipt = self.store.db.execute("SELECT rev, recorded_at FROM receipts WHERE request_id = 'r1'").fetchone()
        self.assertEqual(receipt["rev"], 3)

    def test_after_expiry_cas_rejects_the_late_replay(self):
        """Documentation rule of 3.5: pass expected_revision so a replay after 30 days is rejected."""
        args = dict(kind="checkpoint", request_id="c1", key="task:a", body='{"step":1}', expected_revision=0)
        self.ok(self.alice.save(**args))
        self.clock.now += 29 * MICROS_PER_DAY
        self.assertEqual(self.ok(self.alice.save(**args))["rev"], 1)   # still within retention: replay
        self.clock.now += 1 * MICROS_PER_DAY + 1
        before = self.snapshot()
        error = self.err(self.alice.save(**args), "conflict")
        self.assertEqual(error["details"], {"current_revision": 1})
        self.assertEqual(self.snapshot(), before)

    def test_documentation_states_the_rule(self):
        from recall_v2.api import client
        self.assertIn("30 days", client.__doc__)
        self.assertIn("expected_revision", client.__doc__)


if __name__ == "__main__":
    unittest.main()
