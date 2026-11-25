"""Explicit save and retract through Client (spec 3.1, 2.4, 3.4, 4.4, 7.7)."""
from __future__ import annotations

import unittest

from recall_v2.api import Client
from recall_v2.store import Store

from .governance_helpers import GovernanceCase

ITEM_KEYS = {"id", "kind", "scope", "key", "rev", "state", "title", "body", "fact", "valid_from", "valid_to",
             "recorded_at", "accepted_at", "sources", "flags", "pinned", "rank", "score", "trust"}
ENVELOPE_KEYS = {"ok", "schema_version", "op", "as_of", "known_at", "notice", "count", "truncated", "results"}


class SaveTests(GovernanceCase):
    def test_private_save_is_accepted_with_the_spec_item_shape(self):
        envelope = self.alice.save(kind="note", request_id="n1", key="todo", title="Todo", body="buy milk")
        self.assertEqual(set(envelope), ENVELOPE_KEYS)
        self.assertEqual((envelope["op"], envelope["schema_version"], envelope["count"]), ("save", "recall.v2", 1))
        item = self.ok(envelope)
        self.assertEqual(set(item), ITEM_KEYS)
        self.assertEqual((item["state"], item["rev"], item["scope"]), ("accepted", 1, {"type": "private", "id": "alice"}))
        self.assertEqual(item["accepted_at"], item["recorded_at"])
        self.assertEqual((item["trust"], item["rank"], item["score"], item["flags"]), ("untrusted", None, None, []))
        self.assertEqual(envelope["as_of"], item["recorded_at"])

    def test_project_save_is_a_candidate_until_approved(self):
        item = self.project_note(self.alice, "plan", "ship on friday")
        self.assertEqual((item["state"], item["accepted_at"]), ("candidate", None))
        self.assertEqual(item["scope"], {"type": "project", "id": "proj"})
        self.assertEqual(self.ok(self.bob.lookup(query="friday")), [])
        approved = self.ok(self.op.review_approve(item["id"], 1))
        self.assertEqual(approved["state"], "accepted")
        self.assertEqual(self.ok(self.bob.lookup(query="friday"))["id"], item["id"])

    def test_non_member_project_save_is_not_found(self):
        self.err(self.carol.save(kind="note", request_id="x", scope="project", project="proj", key="k", body="b"),
                 "not_found")
        self.err(self.carol.save(kind="note", request_id="x", scope="project", project="nope", key="k", body="b"),
                 "not_found")

    def test_owner_private_fact_save_supersedes(self):
        first = self.private_fact(self.alice, "ann")
        second = self.private_fact(self.alice, "bob")
        self.assertEqual((second["id"], second["rev"], second["state"]), (first["id"], 2, "accepted"))
        self.assertEqual(second["fact"]["supersedes"], 1)
        current = self.ok(self.alice.lookup(subject="svc", predicate="owner"))
        self.assertEqual((current["rev"], current["fact"]["value"]), (2, "bob"))
        self.assertEqual(self.count("conflicts"), 0)   # a forward change is supersession, not a conflict

    def test_back_dated_private_save_is_accepted_with_auto_resolved_conflicts(self):
        first = self.private_fact(self.alice, "ann")
        start = self.revision(first["id"], 1)["valid_from"]
        third = self.private_fact(self.alice, "carl", valid_from=start - 1000, valid_to=start + 10 ** 12)
        self.assertEqual(third["state"], "accepted")
        rows = self.store.db.execute("SELECT rev_a, rev_b, status FROM conflicts").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, 2, "auto_resolved")])
        # Later acceptance wins (2.5).
        self.assertEqual(self.ok(self.alice.lookup(subject="svc"))["fact"]["value"], "carl")
        self.assertEqual(self.ok(self.op.conflict_list(status="open")), [])

    def test_retract_private_and_project(self):
        fact = self.private_fact(self.alice, "ann")
        retracted = self.ok(self.alice.retract(item_id=fact["id"], rev=1, request_id="rt"))
        self.assertEqual((retracted["rev"], retracted["state"], retracted["fact"]["op"]), (2, "accepted", "retract"))
        self.assertEqual(retracted["fact"]["supersedes"], 1)
        self.assertEqual(self.ok(self.alice.lookup(subject="svc")), [])

        shared = self.project_fact(self.alice, "team-a")
        self.ok(self.op.review_approve(shared["id"], 1))
        candidate = self.ok(self.bob.retract(item_id=shared["id"], rev=1, request_id="rt"))
        self.assertEqual((candidate["state"], candidate["fact"]["op"]), ("candidate", "retract"))
        self.assertEqual(self.ok(self.bob.lookup(subject="svc", scope="project"))["fact"]["value"], "team-a")
        self.ok(self.op.review_approve(shared["id"], 2))
        self.assertEqual(self.ok(self.bob.lookup(subject="svc", scope="project")), [])

    def test_retract_errors(self):
        fact = self.private_fact(self.alice, "ann")
        self.err(self.bob.retract(item_id=fact["id"], rev=1, request_id="x"), "not_found")   # not visible
        self.err(self.alice.retract(item_id=fact["id"], rev=9, request_id="x"), "not_found")
        note = self.ok(self.alice.save(kind="note", request_id="n", key="n", body="b"))
        self.err(self.alice.retract(item_id=note["id"], rev=1, request_id="y"), "invalid_input")


class ErrorCodeTests(GovernanceCase):
    def test_codes_for_the_write_path(self):
        self.err(self.alice.save(kind="memo", request_id="a", key="k", body="b"), "invalid_input")
        self.err(self.alice.save(kind="note", request_id="bad id", key="k", body="b"), "invalid_input")
        self.err(self.alice.save(kind="checkpoint", request_id="a", key="k", body="{}"), "invalid_input")
        self.err(self.alice.save(kind="note", request_id="a", key="k", body="b", valid_from="2026-11-25T00:00:00"),
                 "invalid_input")

        self.ok(self.alice.save(kind="note", request_id="a", key="k", body="one"))
        error = self.err(self.alice.save(kind="note", request_id="b", key="k", body="two", expected_revision=0),
                         "conflict")
        self.assertEqual(error["details"], {"current_revision": 1})
        self.err(self.alice.save(kind="note", request_id="a", key="k", body="other"), "request_id_conflict")

        unknown = Client.open(self.store, "not-a-token")
        self.err(unknown.save(kind="note", request_id="a", key="k", body="b"), "unauthorized")
        self.ok(self.op.principal_revoke("bob"))
        self.err(self.bob.save(kind="note", request_id="a", key="k", body="b"), "unauthorized")
        self.err(self.bob.lookup(query="b"), "unauthorized")

    def test_busy_after_retries(self):
        other = Store.open(self.path, clock=self.clock)
        self.addCleanup(other.close)
        self.store.db.execute("PRAGMA busy_timeout = 0")
        other.db.execute("BEGIN IMMEDIATE")
        try:
            self.err(self.alice.save(kind="note", request_id="a", key="k", body="b"), "busy")
        finally:
            other.db.execute("ROLLBACK")
        self.ok(self.alice.save(kind="note", request_id="a", key="k", body="b"))

    def test_times_are_rfc3339(self):
        item = self.ok(self.alice.save(kind="note", request_id="a", key="k", body="b",
                                       valid_from="2026-11-25T09:30:00+01:00"))
        self.assertEqual(item["valid_from"], "2026-11-25T08:30:00.000000Z")


if __name__ == "__main__":
    unittest.main()
