"""A principal call never reaches an operator function (spec 5 permissions, 7.4)."""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

from recall_v2.api import Client, Operator
from recall_v2.core.errors import RecallError
from recall_v2.governance import authority, conflicts, review
from recall_v2.store import Store

from .governance_helpers import GovernanceCase

OPERATOR_FUNCTIONS = [
    (review, "list_candidates", ()), (review, "approve", ("I", 1)), (review, "reject", ("I", 1)),
    (review, "edit", ("I", 1)), (conflicts, "list_conflicts", ()), (conflicts, "resolve", ("C", "reject")),
]
STORE_OPERATOR_METHODS = ("principal_add", "principal_rotate", "principal_revoke", "project_add", "member_add",
                          "member_remove", "_review_in", "review_approve", "review_reject", "delete_item")


def public_methods(cls) -> set[str]:
    return {name for name, _ in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")}


class SeparationTests(GovernanceCase):
    def test_client_and_operator_share_no_methods(self):
        shared = (public_methods(Client) & public_methods(Operator)) - {"open", "close"}
        self.assertEqual(shared, set())
        for name in public_methods(Client):
            self.assertFalse(name.startswith(("review", "conflict", "principal", "member", "project", "audit")), name)

    def test_no_client_method_accepts_a_principal(self):
        for name in public_methods(Client) - {"open", "close"}:
            params = inspect.signature(getattr(Client, name)).parameters
            self.assertFalse({"principal", "caller", "actor", "reviewer"} & set(params), name)

    def test_operator_functions_refuse_anything_but_an_operator_session(self):
        principal = self.store.authenticate(self.tokens["alice"])
        for target in (self.alice, principal, self.store, None, "~operator"):
            for module, name, args in OPERATOR_FUNCTIONS:
                with self.assertRaises(RecallError) as raised:
                    getattr(module, name)(target, *args)
                self.assertEqual(raised.exception.code, "forbidden", (target, name))
        with self.assertRaises(RecallError) as raised:
            authority.OperatorSession(self.store, object())
        self.assertEqual(raised.exception.code, "forbidden")

    def test_principal_paths_never_reach_operator_code(self):
        cand = self.project_note(self.alice, "plan", "text")
        fact = self.private_fact(self.alice, "v")
        guards = [mock.patch.object(Store, name, side_effect=AssertionError(name)) for name in STORE_OPERATOR_METHODS]
        guards += [mock.patch.object(module, name, side_effect=AssertionError(name))
                   for module, name, _ in OPERATOR_FUNCTIONS]
        guards.append(mock.patch.object(authority, "require_operator", side_effect=AssertionError("operator")))
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        envelopes = [
            self.alice.save(kind="note", request_id="s1", key="k", body="b"),
            self.alice.save(kind="note", request_id="s2", scope="project", project="proj", key="k2", body="b"),
            self.alice.save(kind="fact", request_id="s3", fact={"subject": "a", "predicate": "b", "value": 1}),
            self.alice.retract(item_id=fact["id"], rev=1, request_id="s4"),
            self.alice.lookup(query="text"), self.alice.lookup(subject="svc"),
            self.alice.bundle(project="proj"), self.alice.get(item_id=cand["id"], rev=1),
            self.alice.history(item_id=cand["id"]), self.alice.pin(item_id=fact["id"]),
            self.alice.unpin(item_id=fact["id"]),
            self.carol.save(kind="note", request_id="s5", scope="project", project="proj", key="k", body="b"),
        ]
        for envelope in envelopes:
            if not envelope["ok"]:
                self.assertIn(envelope["error"]["code"], {"not_found"}, envelope)
        self.assertEqual(sum(e["ok"] for e in envelopes), len(envelopes) - 1)

    def test_client_cannot_approve_its_own_candidate(self):
        cand = self.project_note(self.alice, "plan", "text")
        self.assertFalse(hasattr(self.alice, "review_approve"))
        self.assertEqual(self.revision(cand["id"], 1)["state"], "candidate")


if __name__ == "__main__":
    unittest.main()
