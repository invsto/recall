"""Expiry (spec 6): a new revision sets valid_to; expired items leave default reads, stay readable at an
earlier as_of and in history, appear in expire_check, and are never deleted."""
from __future__ import annotations

import unittest

from recall_v2.core import timefmt
from recall_v2.lifecycle import expiry

from .governance_helpers import GovernanceCase

DAY = timefmt.MICROS_PER_DAY


class ExpiryTests(GovernanceCase):
    def note(self, client, key, body, **kwargs):
        return self.ok(client.save(kind="note", request_id=f"n-{key}-{self.clock.now}", key=key, body=body, **kwargs))

    def freeze(self, at: int) -> None:
        self.clock.now, self.clock.step = at, 0

    def lookup_ids(self, client, **kwargs):
        return [r["id"] for r in self.results(client.lookup(**kwargs))]

    def test_boundary_valid_to_equal_to_now_is_expired(self):
        start = self.clock.now
        end = start + DAY
        item = self.note(self.alice, "walrus", "walrus timetable", valid_to=end)
        self.freeze(end - 1)
        self.assertEqual(self.lookup_ids(self.alice, query="walrus"), [item["id"]])
        self.assertEqual(self.ok(self.op.expire_check()), [])
        self.freeze(end)
        self.assertEqual(self.lookup_ids(self.alice, query="walrus"), [])
        self.err(self.alice.get(item_id=item["id"]), "not_found")
        listed = self.results(self.op.expire_check())
        self.assertEqual([(e["item_id"], e["expired_at"]) for e in listed], [(item["id"], timefmt.format_rfc3339(end))])

    def test_expire_writes_a_new_revision_and_deletes_nothing(self):
        item = self.note(self.alice, "plan", "zebra crossing plan")
        at = self.clock.now + DAY
        saved = self.ok(self.alice.expire(item_id=item["id"], request_id="e1", at=at))
        self.assertEqual(saved["rev"], 2)
        self.assertEqual(saved["valid_to"], timefmt.format_rfc3339(at))
        self.assertEqual(saved["body"], "zebra crossing plan")
        self.assertEqual(self.revision(item["id"], 1)["body"], "zebra crossing plan")   # rev 1 untouched
        self.assertEqual(self.revision(item["id"], 1)["valid_to"], None)
        # replay returns the same revision
        self.assertEqual(self.ok(self.alice.expire(item_id=item["id"], request_id="e1", at=at))["rev"], 2)
        self.freeze(at)
        self.assertEqual(self.lookup_ids(self.alice, query="zebra"), [])
        self.assertEqual(self.lookup_ids(self.alice, query="zebra", as_of=at - 1), [item["id"]])  # as_of option
        history = self.results(self.alice.history(item_id=item["id"]))
        self.assertEqual([h["rev"] for h in history], [1, 2])
        self.assertTrue(all(not h["current"] for h in history))
        self.assertEqual([e["item_id"] for e in self.results(self.op.expire_check())], [item["id"]])
        self.assertEqual(self.count("items", "id = ? AND deleted_at IS NULL", (item["id"],)), 1)
        actions = [r[0] for r in self.store.db.execute("SELECT action FROM audit WHERE item_id = ? ORDER BY id",
                                                       (item["id"],))]
        self.assertEqual(actions, ["save", "expire"])

    def test_fact_expiry_hides_value_from_at_and_keeps_history(self):
        fact = self.private_fact(self.alice, "team-blue")
        at = self.clock.now + DAY
        saved = self.ok(self.alice.expire(item_id=fact["id"], request_id="ef", at=at))
        self.assertEqual(saved["fact"]["op"], "retract")
        self.assertEqual(saved["valid_from"], timefmt.format_rfc3339(at))
        self.freeze(at - 1)
        self.assertEqual([r["fact"]["value"] for r in self.results(self.alice.lookup(subject="svc"))], ["team-blue"])
        self.assertEqual(self.ok(self.op.expire_check()), [])
        self.freeze(at)
        self.assertEqual(self.results(self.alice.lookup(subject="svc")), [])
        self.assertEqual([r["fact"]["value"] for r in self.results(self.alice.lookup(subject="svc", as_of=at - 1))],
                         ["team-blue"])
        listed = self.results(self.op.expire_check())
        self.assertEqual([(e["item_id"], e["kind"]) for e in listed], [(fact["id"], "fact")])
        # a later value is not hidden by the expiry
        self.freeze(at + DAY)
        self.private_fact(self.alice, "team-green")
        self.assertEqual([r["fact"]["value"] for r in self.results(self.alice.lookup(subject="svc"))], ["team-green"])
        self.assertEqual(self.ok(self.op.expire_check()), [])

    def test_fact_saved_with_valid_to_is_listed(self):
        start = self.clock.now
        fact = self.private_fact(self.alice, "x", valid_to=start + DAY)
        self.freeze(start + DAY)
        self.assertEqual([e["item_id"] for e in self.results(self.op.expire_check())], [fact["id"]])

    def test_retraction_is_not_expiry(self):
        fact = self.private_fact(self.alice, "x")
        self.ok(self.alice.retract(item_id=fact["id"], rev=1, request_id="r"))
        self.assertEqual(self.ok(self.op.expire_check()), [])

    def test_operator_policy_expiry_and_project_candidate(self):
        cand = self.project_note(self.alice, "doc", "otter notes")
        self.ok(self.op.review_approve(cand["id"], 1))
        at = self.clock.now + DAY
        # a member's expiry of a project item is a candidate for review
        proposed = self.ok(self.bob.expire(item_id=cand["id"], request_id="b1", at=at))
        self.assertEqual(proposed["state"], "candidate")
        self.err(self.carol.expire(item_id=cand["id"], request_id="c1"), "not_found")
        policy = self.ok(self.op.expire_item(cand["id"], at=at))
        self.assertEqual(policy["state"], "accepted")
        self.freeze(at)
        self.assertEqual(self.lookup_ids(self.alice, query="otter"), [])

    def test_invalid_expiry(self):
        item = self.note(self.alice, "k", "body", valid_from=self.clock.now)
        start = self.revision(item["id"], 1)["valid_from"]
        self.err(self.alice.expire(item_id=item["id"], request_id="x1", at=start), "invalid_input")
        self.err(self.alice.expire(item_id="01ARZ3NDEKTSV4RRFFQ69G5FAV", request_id="x2"), "not_found")

    def test_expire_check_is_operator_only(self):
        from recall_v2.core.errors import RecallError
        for target in (self.alice, self.store, None):
            with self.assertRaises(RecallError) as raised:
                expiry.expire_check(target)
            self.assertEqual(raised.exception.code, "forbidden")
        self.assertFalse(hasattr(self.alice, "expire_check"))


if __name__ == "__main__":
    unittest.main()
