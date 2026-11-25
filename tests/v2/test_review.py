"""Review queue: approve, reject, edit-then-approve, stale_review, stale listing (spec 3.6, 6)."""
from __future__ import annotations

import json
import unittest

from .governance_helpers import MICROS_PER_DAY, GovernanceCase


class ReviewTests(GovernanceCase):
    def test_approve(self):
        cand = self.project_note(self.alice, "plan", "ship friday")
        item = self.ok(self.op.review_approve(cand["id"], 1))
        row = self.revision(cand["id"], 1)
        self.assertEqual((item["state"], row["reviewed_by"]), ("accepted", "~operator"))
        self.assertEqual(row["accepted_at"], row["reviewed_at"])
        self.assertEqual(self.ok(self.bob.get(item_id=cand["id"]))["rev"], 1)
        self.assertEqual(self.ok(self.op.review_list()), [])
        self.err(self.op.review_approve(cand["id"], 1), "stale_review")   # already reviewed
        self.err(self.op.review_approve(cand["id"], 7), "not_found")

    def test_reject(self):
        cand = self.project_note(self.alice, "plan", "ship friday")
        item = self.ok(self.op.review_reject(cand["id"], 1, note="not now"))
        row = self.revision(cand["id"], 1)
        self.assertEqual((item["state"], row["review_note"], row["accepted_at"]), ("rejected", "not now", None))
        self.assertEqual(self.ok(self.op.review_list()), [])
        self.err(self.bob.get(item_id=cand["id"]), "not_found")
        self.err(self.op.review_approve(cand["id"], 1), "stale_review")

    def test_accepted_on_write_cannot_be_reviewed(self):
        private = self.ok(self.alice.save(kind="note", request_id="p", key="k", body="b"))
        self.err(self.op.review_approve(private["id"], 1), "invalid_input")

    def test_edit_then_approve_records_the_editor(self):
        cand = self.project_note(self.alice, "plan", "ship firday", title="Plan")
        listed = self.ok(self.op.review_list())
        self.assertEqual((listed["author"], listed["kind"], listed["scope"]), ("alice", "note", {"type": "project", "id": "proj"}))
        self.assertIn("+ship firday", listed["diff"])
        edited = self.ok(self.op.review_edit(cand["id"], 1, body="ship friday"))
        self.assertEqual((edited["rev"], edited["state"], edited["body"], edited["title"]), (2, "accepted", "ship friday", "Plan"))
        new = self.revision(cand["id"], 2)
        self.assertEqual((new["actor_id"], new["reviewed_by"]), ("~operator", "~operator"))
        self.assertEqual(json.loads(new["meta_json"]), {"base_rev": 0, "edited_from": 1})
        old = self.revision(cand["id"], 1)
        self.assertEqual((old["state"], old["review_note"]), ("rejected", "edited"))
        self.assertEqual(self.ok(self.bob.lookup(query="friday"))["rev"], 2)
        actions = [r[0] for r in self.store.db.execute("SELECT action FROM audit WHERE item_id = ? ORDER BY id",
                                                       (cand["id"],))]
        self.assertEqual(actions, ["save", "review_edit", "review_reject", "review_approve"])

    def test_edit_fact_value_and_interval(self):
        cand = self.project_fact(self.alice, "team-a", valid_from="2026-01-01T00:00:00.000000Z")
        edited = self.ok(self.op.review_edit(cand["id"], 1, value="team-b", valid_to="2026-06-01T00:00:00.000000Z"))
        self.assertEqual((edited["fact"]["value"], edited["valid_to"]), ("team-b", "2026-06-01T00:00:00.000000Z"))
        self.err(self.op.review_edit(cand["id"], 1, value="x"), "stale_review")   # already edited
        other = self.project_fact(self.bob, "team-c", subject="db")
        self.err(self.op.review_edit(other["id"], 1, body="x"), "invalid_input")

    def test_stale_approval_after_concurrent_change_and_after_edit(self):
        a = self.project_note(self.alice, "plan", "alice version")
        b = self.project_note(self.bob, "plan", "bob version")
        self.assertEqual((a["id"], a["rev"], b["rev"]), (b["id"], 1, 2))
        self.ok(self.op.review_edit(b["id"], 2, body="bob version, edited"))
        error = self.err(self.op.review_approve(a["id"], 1), "stale_review")
        self.assertEqual(error["details"], {"current_revision": 3})
        self.err(self.op.review_edit(a["id"], 1, body="x"), "stale_review")
        self.assertEqual(self.revision(a["id"], 1)["state"], "candidate")   # failed approval changed nothing

    def test_membership_removed_before_approval_fails(self):
        cand = self.project_note(self.bob, "plan", "bob version")
        self.ok(self.op.member_remove("bob", "proj"))
        self.err(self.op.review_approve(cand["id"], 1), "stale_review")
        self.err(self.op.review_edit(cand["id"], 1, body="x"), "stale_review")
        self.ok(self.op.member_add("bob", "proj"))
        self.ok(self.op.review_approve(cand["id"], 1))

    def test_revoked_author_fails(self):
        cand = self.project_note(self.bob, "plan", "bob version")
        self.ok(self.op.principal_revoke("bob"))
        self.err(self.op.review_approve(cand["id"], 1), "stale_review")
        self.ok(self.op.review_reject(cand["id"], 1))   # rejection needs no basis

    def test_stale_candidates_are_listed_never_deleted(self):
        old = self.project_note(self.alice, "old", "old idea")
        self.clock.now += 20 * MICROS_PER_DAY
        self.project_note(self.alice, "new", "new idea")
        self.assertEqual(self.ok(self.op.review_list(stale=True)), [])
        self.clock.now += 11 * MICROS_PER_DAY
        stale = self.ok(self.op.review_list(stale=True))
        self.assertEqual((stale["item_id"], stale["stale"]), (old["id"], True))
        self.assertGreaterEqual(stale["age_seconds"], 31 * 86_400)
        self.assertEqual(len(self.ok(self.op.review_list())), 2)
        self.clock.now += 400 * MICROS_PER_DAY
        self.ok(self.op.review_list(stale=True))
        self.assertEqual(self.count("item_revisions", "state = 'candidate'"), 2)


if __name__ == "__main__":
    unittest.main()
