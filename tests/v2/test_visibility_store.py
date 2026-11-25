"""Store-level attack set: cross-principal, wrong-project, removed-member, candidate, deleted, expired and
revoked-token probes. Every read path goes through the one visibility predicate; the leak count must be 0."""
from __future__ import annotations

import unittest

from recall_v2.core.errors import RecallError

from .helpers import StoreTestCase, T0


class VisibilityAttackSet(StoreTestCase):
    def setUp(self):
        s = self.store = self.new_store()
        self.alice = self.add_principal(s, "alice")       # member of p1
        self.bob = self.add_principal(s, "bob")           # member of p1
        self.outsider = self.add_principal(s, "outsider")  # no membership
        self.removed = self.add_principal(s, "removed")   # member of p1, later removed
        self.revoked = self.add_principal(s, "revoked")   # member of p1, later revoked
        self.other = self.add_principal(s, "other")       # member of p2 only
        s.project_add("p1")
        s.project_add("p2")
        for name in ("alice", "bob", "removed", "revoked"):
            s.member_add(name, "p1")
        s.member_add("other", "p2")

        fact = {"subject": "service x", "predicate": "owner"}
        self.items = {}
        self.items["alice_private_note"] = s.save(self.alice, kind="note", request_id="1", key="a-note", body="A")
        self.items["alice_private_fact"] = s.save(self.alice, kind="fact", request_id="2", fact={**fact, "value": "a"})
        self.items["bob_private_fact"] = s.save(self.bob, kind="fact", request_id="3", fact={**fact, "value": "b"})
        accepted = s.save(self.bob, kind="note", request_id="4", key="p-note", body="P", scope="project", project="p1")
        s.review_approve(**accepted)
        self.items["p1_accepted_note"] = accepted
        project_fact = s.save(self.bob, kind="fact", request_id="5", scope="project", project="p1",
                              fact={**fact, "value": "p"})
        s.review_approve(**project_fact)
        self.items["p1_accepted_fact"] = project_fact
        self.items["p1_bob_candidate"] = s.save(self.bob, kind="note", request_id="6", key="cand", body="C",
                                                scope="project", project="p1")
        p2 = s.save(self.other, kind="note", request_id="7", key="p2-note", body="Q", scope="project", project="p2")
        s.review_approve(**p2)
        self.items["p2_accepted_note"] = p2
        deleted = s.save(self.alice, kind="note", request_id="8", key="gone", body="D")
        s.delete_item(deleted["item_id"])
        self.items["alice_deleted_note"] = deleted
        self.items["alice_expired_note"] = s.save(self.alice, kind="note", request_id="9", key="old", body="E",
                                                  valid_from=T0 - 10, valid_to=T0)
        s.member_remove("removed", "p1")
        s.principal_revoke("revoked")

        # Expected visible item sets (spec 4.1 and 5), excluding deleted items.
        project_p1 = {"p1_accepted_note", "p1_accepted_fact"}
        self.expected_items = {
            "alice": {"alice_private_note", "alice_private_fact", "alice_expired_note"} | project_p1,
            "bob": {"bob_private_fact", "p1_bob_candidate"} | project_p1,
            "outsider": set(),
            "removed": set(),
            "other": {"p2_accepted_note"},
        }

    def principals(self):
        return {"alice": self.alice, "bob": self.bob, "outsider": self.outsider, "removed": self.removed,
                "other": self.other}

    def probe(self, func, **kwargs):
        try:
            return func(**kwargs)
        except RecallError as exc:
            return exc

    def test_attack_set_leak_count_is_zero(self):
        leaks = []
        probes = 0
        for name, principal in self.principals().items():
            allowed = self.expected_items[name]
            ids = {self.items[label]["item_id"]: label for label in allowed}
            visible = set(self.store.visible_item_ids(principal))
            if visible != set(ids):
                leaks.append((name, "visible_item_ids", sorted(visible ^ set(ids))))
            for label, ref in self.items.items():
                item_id = ref["item_id"]
                for func, kwargs in ((self.store.history, {}), (self.store.get, {"rev": ref["rev"]}),
                                     (self.store.get, {"as_of": T0 - 5})):
                    probes += 1
                    result = self.probe(func, principal=principal, item_id=item_id, **kwargs)
                    should_see = label in allowed
                    if isinstance(result, RecallError):
                        if result.code != "not_found":
                            leaks.append((name, label, func.__name__, result.code))
                        continue
                    if not should_see:
                        leaks.append((name, label, func.__name__, "returned data"))
            # Fact lookup: only the caller's own private fact and its projects' accepted facts.
            probes += 1
            facts = self.store.lookup_facts(principal, subject="SERVICE X")
            got = {row["id"] for row in facts}
            want = {self.items[l]["item_id"] for l in allowed if l.endswith("fact")}
            if got != want:
                leaks.append((name, "lookup_facts", sorted(got ^ want)))
        self.assertGreater(probes, 100)
        self.assertEqual(leaks, [], f"{len(leaks)} leaks")

    def test_candidates_are_visible_only_to_their_author(self):
        candidate = self.items["p1_bob_candidate"]
        self.assertEqual(self.store.get(self.bob, item_id=candidate["item_id"], rev=1)["state"], "candidate")
        self.assertCode("not_found", self.store.get, self.alice, item_id=candidate["item_id"], rev=1)
        self.assertCode("not_found", self.store.history, self.alice, item_id=candidate["item_id"])
        # A pending candidate on a visible accepted item is hidden from other members' history.
        note = self.items["p1_accepted_note"]
        self.store.save(self.bob, kind="note", request_id="10", key="p-note", body="P2", scope="project", project="p1")
        self.assertEqual([r["rev"] for r in self.store.history(self.alice, item_id=note["item_id"])], [1])
        self.assertEqual([r["rev"] for r in self.store.history(self.bob, item_id=note["item_id"])], [1, 2])

    def test_lookup_orders_private_first_and_returns_one_per_key(self):
        rows = self.store.lookup_facts(self.alice, subject="service x", predicate="owner")
        self.assertEqual([(r["scope"]["type"], r["fact"]["value"]) for r in rows], [("private", "a"), ("project", "p")])

    def test_not_found_is_indistinguishable(self):
        missing = self.probe(self.store.get, principal=self.outsider, item_id="0" * 26)
        hidden = self.probe(self.store.get, principal=self.outsider, item_id=self.items["p1_accepted_note"]["item_id"])
        self.assertEqual((missing.code, missing.message, missing.details), (hidden.code, hidden.message, hidden.details))

    def test_write_paths_respect_scope(self):
        self.assertCode("not_found", self.store.save, self.outsider, kind="note", request_id="x", key="k", body="b",
                        scope="project", project="p1")
        self.assertCode("not_found", self.store.save, self.removed, kind="note", request_id="x", key="k", body="b",
                        scope="project", project="p1")
        self.assertCode("not_found", self.store.retract, self.bob, item_id=self.items["alice_private_fact"]["item_id"],
                        rev=1, request_id="x")
        self.assertCode("not_found", self.store.retract, self.outsider,
                        item_id=self.items["p1_accepted_fact"]["item_id"], rev=1, request_id="x")
        # Saving the same key privately creates a separate item; scope is never shared across principals.
        out = self.store.save(self.bob, kind="note", request_id="y", key="a-note", body="B")
        self.assertNotEqual(out["item_id"], self.items["alice_private_note"]["item_id"])

    def test_revoked_and_unknown_tokens_are_unauthorized(self):
        self.assertCode("unauthorized", self.store.get, self.revoked, item_id=self.items["p1_accepted_note"]["item_id"])
        self.assertCode("unauthorized", self.store.visible_item_ids, self.revoked)
        self.assertCode("unauthorized", self.store.authenticate, "not-a-token")

    def test_membership_changes_apply_on_next_query(self):
        note = self.items["p1_accepted_note"]["item_id"]
        self.assertCode("not_found", self.store.get, self.outsider, item_id=note)
        self.store.member_add("outsider", "p1")
        self.assertEqual(self.store.get(self.outsider, item_id=note)["rev"], 1)
        self.store.member_remove("outsider", "p1")
        self.assertCode("not_found", self.store.get, self.outsider, item_id=note)


if __name__ == "__main__":
    unittest.main()
