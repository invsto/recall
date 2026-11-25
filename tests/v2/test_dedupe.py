"""Exact dedupe returns the existing revision; near-duplicates are flagged, never merged (spec 3.3)."""
from __future__ import annotations

import unittest

from recall_v2.governance.dedupe import NEAR_DUPLICATE_SIMILARITY

from .governance_helpers import GovernanceCase
from .retrieval_helpers import FakeEmbedder


class ExactDedupeTests(GovernanceCase):
    def test_exact_duplicate_returns_existing_revision_without_a_new_row(self):
        first = self.ok(self.alice.save(kind="note", request_id="a", key="n", title="T", body="hello\r\nworld  "))
        revisions, audit_rows = self.count("item_revisions"), self.count("audit")
        again = self.ok(self.alice.save(kind="note", request_id="b", key="n", title="T", body="hello\nworld"))
        self.assertEqual((again["id"], again["rev"]), (first["id"], 1))
        self.assertEqual((self.count("item_revisions"), self.count("audit")), (revisions, audit_rows))

    def test_candidate_dedupe_only_against_the_callers_own_candidates(self):
        mine = self.project_note(self.alice, "plan", "same text")
        self.assertEqual(self.project_note(self.alice, "plan", "same text")["rev"], mine["rev"])
        # Bob cannot see alice's candidate, so it is neither returned nor revealed to him.
        bobs = self.project_note(self.bob, "plan", "same text")
        self.assertEqual((bobs["id"], bobs["rev"], bobs["state"]), (mine["id"], 2, "candidate"))
        self.assertEqual(self.count("item_revisions", "state = 'candidate'"), 2)


class FactKeyNearDuplicateTests(GovernanceCase):
    def test_same_fact_key_open_candidate_is_flagged_not_merged(self):
        first = self.project_fact(self.alice, "team-a")
        self.assertEqual(first["flags"], [])
        second = self.project_fact(self.bob, "team-b")
        self.assertEqual((second["id"], second["rev"], second["flags"]), (first["id"], 2, ["dedupe"]))
        rows = self.store.db.execute("SELECT item_id, rev, type, other_item_id FROM flags").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(first["id"], 2, "dedupe", first["id"])])
        listed = self.ok(self.op.review_list())
        self.assertEqual([(c["rev"], [f["type"] for f in c["flags"]]) for c in listed], [(1, []), (2, ["dedupe"])])
        # Never merged: both candidates remain, with their own values.
        values = [r[0] for r in self.store.db.execute("SELECT value_json FROM fact_versions ORDER BY rev")]
        self.assertEqual(values, ['"team-a"', '"team-b"'])


class VectorNearDuplicateTests(GovernanceCase):
    embedder = FakeEmbedder()

    def test_vector_similarity_flags_within_scope_only(self):
        text = "deploy the billing service every friday after the freeze"
        accepted = self.project_note(self.alice, "deploy", text, title="Deploy")
        self.ok(self.op.review_approve(accepted["id"], 1))
        self.ok(self.bob.save(kind="note", request_id="pv", key="private-copy", title="Deploy", body=text))

        similar = self.project_note(self.bob, "deploy-copy", text, title="Deploy")
        self.assertEqual(similar["flags"], ["dedupe"])
        others = [r[0] for r in self.store.db.execute(
            "SELECT other_item_id FROM flags WHERE item_id = ?", (similar["id"],))]
        self.assertEqual(others, [accepted["id"]])   # the private copy in bob's scope is never compared
        unrelated = self.project_note(self.bob, "lunch", "pizza on tuesday", title="Lunch")
        self.assertEqual(unrelated["flags"], [])
        # Not merged: separate items, the original unchanged.
        self.assertNotEqual(similar["id"], accepted["id"])
        self.assertEqual(self.count("item_revisions", "item_id = ?", (accepted["id"],)), 1)
        self.assertEqual(NEAR_DUPLICATE_SIMILARITY, 0.92)

    def test_threshold_is_applied(self):
        from recall_v2.store import embeddings
        a = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        near, far = a + " lambda", "alpha beta gamma delta epsilon"
        [va, vn, vf] = self.embedder.embed([f"T\n{a}", f"T\n{near}", f"T\n{far}"])
        self.assertGreaterEqual(embeddings.cosine(va, vn), 0.92)
        self.assertLess(embeddings.cosine(va, vf), 0.92)
        base = self.project_note(self.alice, "a", a, title="T")
        self.ok(self.op.review_approve(base["id"], 1))
        self.assertEqual(self.project_note(self.alice, "near", near, title="T")["flags"], ["dedupe"])
        self.assertEqual(self.project_note(self.alice, "far", far, title="T")["flags"], [])

if __name__ == "__main__":
    unittest.main()
