"""Conflict creation and explicit resolution; nothing resolves a conflict silently (spec 3.4)."""
from __future__ import annotations

import unittest

from .governance_helpers import MICROS_PER_DAY, GovernanceCase

JAN = "2026-01-01T00:00:00.000000Z"
DEC = "2025-12-01T00:00:00.000000Z"
MAR = "2026-03-01T00:00:00.000000Z"


class ConflictTests(GovernanceCase):
    def setUp(self):
        super().setUp()
        self.fact = self.project_fact(self.alice, "team-a", valid_from=JAN)
        self.ok(self.op.review_approve(self.fact["id"], 1))

    def open_conflicts(self):
        return self.results(self.op.conflict_list(status="open"))

    def test_disagreement_creates_an_open_conflict_forward_change_does_not(self):
        forward = self.project_fact(self.bob, "team-f", valid_from=MAR)
        self.assertEqual(forward["state"], "candidate")
        self.assertEqual(self.count("conflicts"), 0)
        same = self.project_fact(self.bob, "team-a", valid_from=DEC)   # same value: no conflict
        self.assertEqual(self.count("conflicts"), 0)
        clash = self.project_fact(self.bob, "team-b", valid_from=DEC, valid_to=MAR)
        self.assertEqual(clash["state"], "candidate")
        [conflict] = self.open_conflicts()
        self.assertEqual((conflict["rev_a"], conflict["rev_b"], conflict["status"]), (1, clash["rev"], "open"))
        listed = {c["rev"]: c for c in self.results(self.op.review_list())}
        self.assertEqual(listed[clash["rev"]]["conflicts"], [conflict["conflict_id"]])
        self.assertEqual(listed[same["rev"]]["conflicts"], [])

    def test_nothing_auto_resolves_a_conflict(self):
        clash = self.project_fact(self.bob, "team-b", valid_from=DEC, valid_to=MAR)
        [conflict] = self.open_conflicts()
        # Other writes, reads, reviews of other items, failed approvals and time passing leave it open.
        self.project_fact(self.alice, "team-c", valid_from=DEC, valid_to=MAR)
        self.private_fact(self.bob, "mine")
        self.bob.lookup(subject="svc")
        self.bob.bundle(project="proj")
        self.alice.history(item_id=self.fact["id"])
        note = self.project_note(self.alice, "n", "body")
        self.ok(self.op.review_approve(note["id"], 1))
        self.ok(self.op.member_remove("bob", "proj"))
        self.err(self.op.review_approve(clash["id"], clash["rev"]), "stale_review")
        self.ok(self.op.member_add("bob", "proj"))
        self.clock.now += 400 * MICROS_PER_DAY
        self.ok(self.op.review_list(stale=True))
        self.assertIn(conflict["conflict_id"], [c["conflict_id"] for c in self.open_conflicts()])
        self.assertEqual(self.results(self.op.conflict_list(status="resolved")), [])
        self.assertEqual(self.ok(self.bob.lookup(subject="svc", scope="project"))["fact"]["value"], "team-a")

    def test_resolve_supersede(self):
        clash = self.project_fact(self.bob, "team-b", valid_from=DEC, valid_to=MAR)
        [conflict] = self.open_conflicts()
        out = self.ok(self.op.conflict_resolve(conflict["conflict_id"], "supersede"))
        self.assertEqual((out["action"], out["item"]["rev"], out["item"]["state"]), ("supersede", clash["rev"], "accepted"))
        [done] = self.results(self.op.conflict_list(status="resolved"))
        self.assertEqual((done["resolved_by"], done["status"]), ("~operator", "resolved"))
        self.assertEqual(self.ok(self.bob.lookup(subject="svc", as_of="2026-02-01T00:00:00.000000Z"))["fact"]["value"],
                         "team-b")
        self.err(self.op.conflict_resolve(conflict["conflict_id"], "reject"), "stale_review")

    def test_resolve_reject(self):
        clash = self.project_fact(self.bob, "team-b", valid_from=DEC, valid_to=MAR)
        [conflict] = self.open_conflicts()
        out = self.ok(self.op.conflict_resolve(conflict["conflict_id"], "reject"))
        self.assertEqual(out["item"]["state"], "rejected")
        self.assertEqual(self.revision(clash["id"], clash["rev"])["state"], "rejected")
        self.assertEqual(self.open_conflicts(), [])
        self.assertEqual(self.ok(self.bob.lookup(subject="svc", as_of="2026-02-01T00:00:00.000000Z"))["fact"]["value"],
                         "team-a")

    def test_keep_both_only_with_disjoint_intervals(self):
        self.project_fact(self.bob, "team-b", valid_from=DEC, valid_to=MAR)
        [conflict] = self.open_conflicts()
        cid = conflict["conflict_id"]
        self.err(self.op.conflict_resolve(cid, "keep_both"), "invalid_input")            # still overlaps
        self.err(self.op.conflict_resolve(cid, "keep_both", valid_to="2026-01-01T00:00:00.000001Z"), "invalid_input")
        self.err(self.op.conflict_resolve(cid, "supersede", valid_to=JAN), "invalid_input")
        self.assertEqual(len(self.open_conflicts()), 1)
        out = self.ok(self.op.conflict_resolve(cid, "keep_both", valid_to=JAN))   # touching: disjoint
        item = out["item"]
        self.assertEqual((item["state"], item["valid_from"], item["valid_to"], item["fact"]["value"]),
                         ("accepted", DEC, JAN, "team-b"))
        self.assertEqual(self.revision(item["id"], item["rev"])["actor_id"], "~operator")
        self.assertEqual(self.open_conflicts(), [])
        at = lambda ts: self.ok(self.bob.lookup(subject="svc", as_of=ts))["fact"]["value"]
        self.assertEqual((at("2025-12-15T00:00:00.000000Z"), at("2026-02-01T00:00:00.000000Z")), ("team-b", "team-a"))

    def test_private_save_conflict_is_auto_resolved_and_cannot_be_resolved_again(self):
        self.private_fact(self.alice, "x", valid_from=JAN)
        self.private_fact(self.alice, "y", valid_from=DEC, valid_to=MAR)
        [auto] = self.results(self.op.conflict_list(status="auto_resolved"))
        self.assertEqual(auto["scope"], {"type": "private", "id": "alice"})
        self.err(self.op.conflict_resolve(auto["conflict_id"], "reject"), "invalid_input")
        self.err(self.op.conflict_resolve("01AAAAAAAAAAAAAAAAAAAAAAAA", "reject"), "not_found")
        self.err(self.op.conflict_resolve(auto["conflict_id"], "merge"), "invalid_input")


if __name__ == "__main__":
    unittest.main()
