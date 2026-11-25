"""Receipts (idempotent replay, request_id_conflict, expiry) and compare-and-swap (spec 3.1, 3.5)."""
from __future__ import annotations

import json
import unittest

from recall_v2.core.timefmt import MICROS_PER_DAY

from .helpers import StoreTestCase


class ReceiptTests(StoreTestCase):
    def setUp(self):
        self.store = self.new_store()
        self.alice = self.add_principal(self.store, "alice")
        self.bob = self.add_principal(self.store, "bob")

    def count(self, table):
        return self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_replay_returns_the_same_result_without_a_new_row(self):
        args = dict(kind="note", request_id="req-1", key="n1", body="hello")
        first = self.store.save(self.alice, **args)
        revisions, audit_rows = self.count("item_revisions"), self.count("audit")
        self.assertEqual(self.store.save(self.alice, **args), first)
        self.assertEqual((self.count("item_revisions"), self.count("audit")), (revisions, audit_rows))

        fact = dict(kind="fact", request_id="req-2", fact={"subject": "s", "predicate": "p", "value": 1})
        self.assertEqual(self.store.save(self.alice, **fact), self.store.save(self.alice, **fact))
        retract = dict(item_id=self.store.save(self.alice, **fact)["item_id"], rev=1, request_id="req-3")
        self.assertEqual(self.store.retract(self.alice, **retract), self.store.retract(self.alice, **retract))

    def test_same_request_id_different_content_is_request_id_conflict(self):
        self.store.save(self.alice, kind="note", request_id="req-1", key="n1", body="hello")
        self.assertCode("request_id_conflict", self.store.save, self.alice, kind="note", request_id="req-1",
                        key="n1", body="goodbye")
        self.assertCode("request_id_conflict", self.store.save, self.alice, kind="note", request_id="req-1",
                        key="n2", body="hello")
        out = self.store.save(self.alice, kind="fact", request_id="req-2",
                              fact={"subject": "s", "predicate": "p", "value": 1})
        self.assertCode("request_id_conflict", self.store.retract, self.alice, item_id=out["item_id"], rev=1,
                        request_id="req-1")
        # Receipts are per principal: another principal may reuse the request_id.
        self.store.save(self.bob, kind="note", request_id="req-1", key="n1", body="other")

    def test_receipt_expires_after_30_days(self):
        args = dict(kind="checkpoint", request_id="req-1", key="task:a", body='{"step": 1}', expected_revision=0)
        self.assertEqual(self.store.save(self.alice, **args)["rev"], 1)
        self.clock.now += 30 * MICROS_PER_DAY + 10
        # After expiry the replay is a new request; CAS then rejects it.
        error = self.assertCode("conflict", self.store.save, self.alice, **args)
        self.assertEqual(error.details["current_revision"], 1)

    def test_fingerprint_ignores_json_key_order_of_values(self):
        a = self.store.save(self.alice, kind="fact", request_id="r", fact={"subject": "s", "predicate": "p",
                                                                         "value": {"a": 1, "b": 2}})
        self.assertEqual(a, self.store.save(self.alice, kind="fact", request_id="r",
                                            fact={"subject": "s", "predicate": "p", "value": {"b": 2, "a": 1}}))


class CompareAndSwapTests(StoreTestCase):
    def setUp(self):
        self.store = self.new_store()
        self.alice = self.add_principal(self.store, "alice")

    def test_stale_expected_revision_is_conflict(self):
        save = self.store.save
        one = save(self.alice, kind="checkpoint", request_id="a", key="task:x", body='{"s":1}', expected_revision=0)
        two = save(self.alice, kind="checkpoint", request_id="b", key="task:x", body='{"s":2}', expected_revision=1)
        self.assertEqual((one["rev"], two["rev"]), (1, 2))
        error = self.assertCode("conflict", save, self.alice, kind="checkpoint", request_id="c", key="task:x",
                                body='{"s":3}', expected_revision=1)
        self.assertEqual(error.details, {"current_revision": 2})
        error = self.assertCode("conflict", save, self.alice, kind="checkpoint", request_id="d", key="task:new",
                                body='{"s":3}', expected_revision=4)
        self.assertEqual(error.details, {"current_revision": 0})
        # The failed write left no trace and its request_id is reusable.
        self.assertEqual(save(self.alice, kind="checkpoint", request_id="c", key="task:x", body='{"s":3}',
                              expected_revision=2)["rev"], 3)

    def test_optional_cas_for_notes_and_facts(self):
        save = self.store.save
        save(self.alice, kind="note", request_id="a", key="n", body="x")
        self.assertCode("conflict", save, self.alice, kind="note", request_id="b", key="n", body="y",
                        expected_revision=0)
        fact = {"subject": "s", "predicate": "p", "value": 1}
        save(self.alice, kind="fact", request_id="c", fact=fact)
        self.assertCode("conflict", save, self.alice, kind="fact", request_id="d", expected_revision=0,
                        fact={"subject": "S", "predicate": "P", "value": 2})
        self.assertEqual(save(self.alice, kind="fact", request_id="e", expected_revision=1,
                              fact={"subject": "S", "predicate": "P", "value": 2})["rev"], 2)

    def test_required_expected_revision_and_save_rules(self):
        save = self.store.save
        self.assertCode("invalid_input", save, self.alice, kind="checkpoint", request_id="a", key="t", body="{}")
        self.assertCode("invalid_input", save, self.alice, kind="checkpoint", request_id="a", key="t", body="[1]",
                        expected_revision=0)
        self.assertCode("invalid_input", save, self.alice, kind="brief", request_id="a", key="t", body="x",
                        expected_revision=0)   # brief is project-only
        self.assertCode("invalid_input", save, self.alice, kind="note", request_id="a", key="t", body="x" * 4097)
        self.assertCode("invalid_input", save, self.alice, kind="excerpt", request_id="a", key="t", body="x",
                        source_ref="http://insecure")
        self.assertCode("invalid_input", save, self.alice, kind="fact", request_id="a",
                        fact={"subject": "s", "predicate": "p", "value": "v" * 2100})
        self.assertCode("invalid_input", save, self.alice, kind="note", request_id="bad id", key="t", body="x")

    def test_exact_dedupe_returns_existing_revision(self):
        save = self.store.save
        first = save(self.alice, kind="note", request_id="a", key="n", body="hello\r\n")
        self.assertEqual(save(self.alice, kind="note", request_id="b", key="n", body="hello  "), first)
        self.assertEqual(save(self.alice, kind="note", request_id="c", key="n", body="changed")["rev"], 2)


if __name__ == "__main__":
    unittest.main()
