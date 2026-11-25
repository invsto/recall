"""Candidates are visible only to their author and the operator, and never reach a retrieval leg."""
from __future__ import annotations

import unittest

from recall_v2.retrieval import canonical_bytes

from .governance_helpers import GovernanceCase
from .retrieval_helpers import FakeEmbedder


class CandidateVisibilityTests(GovernanceCase):
    embedder = FakeEmbedder()

    def setUp(self):
        super().setUp()
        note = self.project_note(self.alice, "runbook", "kraken deployment runbook", title="kraken runbook")
        self.ok(self.op.review_approve(note["id"], 1))
        fact = self.project_fact(self.alice, "team-a", subject="kraken", predicate="owner")
        self.ok(self.op.review_approve(fact["id"], 1))
        self.note_id, self.fact_id = note["id"], fact["id"]
        self.ok(self.bob.save(kind="note", request_id="bp", key="mine", title="kraken private", body="kraken notes"))

    def views(self, client, when):
        at = dict(as_of=when, known_at=when)
        return [canonical_bytes(envelope) for envelope in (
            client.lookup(query="kraken", explain=True, **at),
            client.lookup(query="kraken runbook octopus", explain=True, project="proj", **at),
            client.lookup(subject="kraken", **at),
            client.lookup(predicate="owner", scope="project", **at),
            client.bundle(project="proj", as_of=at["as_of"]),
        )]

    def test_candidates_reach_no_leg_and_no_other_principal(self):
        explained = self.bob.lookup(query="kraken", explain=True)
        self.assertIn("vector", explained["results"][0]["explain"])   # both legs are on
        when = self.clock.now + 10 ** 9   # after every write below, so a leaked revision would count
        before = {name: self.views(client, when) for name, client in (("alice", self.alice), ("bob", self.bob))}
        # Candidates: an edit of each accepted item, a new item, a retraction; all by alice.
        n2 = self.project_note(self.alice, "runbook", "kraken octopus deployment runbook v2", title="kraken runbook")
        f2 = self.project_fact(self.alice, "team-octopus", subject="kraken", predicate="owner")
        n3 = self.project_note(self.alice, "fresh", "kraken octopus brand new", title="kraken octopus")
        r = self.ok(self.alice.retract(item_id=self.fact_id, rev=1, request_id="rt"))
        self.assertEqual({c["state"] for c in (n2, f2, n3, r)}, {"candidate"})
        after = {name: self.views(client, when) for name, client in (("alice", self.alice), ("bob", self.bob))}
        self.assertEqual(after, before)

        # get and history: the author sees her candidates; another member and a non-member do not.
        self.assertEqual(self.ok(self.alice.get(item_id=n3["id"], rev=1))["state"], "candidate")
        self.err(self.bob.get(item_id=n3["id"], rev=1), "not_found")
        self.err(self.bob.get(item_id=n3["id"]), "not_found")
        self.err(self.bob.history(item_id=n3["id"]), "not_found")
        self.err(self.carol.get(item_id=n3["id"], rev=1), "not_found")
        self.err(self.bob.get(item_id=self.note_id, rev=2), "not_found")
        self.assertEqual([i["rev"] for i in self.results(self.bob.history(item_id=self.fact_id))], [1])
        self.assertEqual([i["rev"] for i in self.results(self.alice.history(item_id=self.fact_id))], [1, 2, 3])
        # The operator sees every candidate.
        self.assertEqual(len(self.results(self.op.review_list())), 4)

    def test_after_approval_the_item_is_visible(self):
        n3 = self.project_note(self.alice, "fresh", "kraken octopus brand new", title="kraken octopus")
        self.assertNotIn(n3["id"], [i["id"] for i in self.results(self.bob.lookup(query="octopus"))])
        self.ok(self.op.review_approve(n3["id"], 1))
        self.assertIn(n3["id"], [i["id"] for i in self.results(self.bob.lookup(query="octopus"))])


if __name__ == "__main__":
    unittest.main()
