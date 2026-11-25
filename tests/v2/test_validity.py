"""Intervals, default validity, and explicit current-truth cases (spec 2.4, 2.5, 3.4)."""
from __future__ import annotations

import unittest

from recall_v2.core.validity import (FactRevision, Revision, check_interval, conflicts_with, contains,
                                     current_fact, current_other, default_valid_to, effective_interval, overlaps)
from recall_v2.core.errors import RecallError

from .helpers import StoreTestCase, T0


def A(rev, vf, vt=None, acc=None, state="accepted"):
    return FactRevision(rev, "assert", vf, vt, state, acc if state == "accepted" else None)


def R(rev, vf, vt=None, acc=None):
    return FactRevision(rev, "retract", vf, vt, "accepted", acc)


class IntervalTests(unittest.TestCase):
    def test_overlap_truth_table(self):
        cases = [
            ((0, 10, 5, 15), True),
            ((0, 10, 10, 20), False),     # touching
            ((10, 20, 0, 10), False),     # touching, reversed
            ((0, 10, 20, 30), False),
            ((0, None, 100, 200), True),  # open-ended
            ((0, None, 5, None), True),
            ((0, 5, 5, None), False),     # touching an open interval
            ((5, None, 0, 5), False),
            ((0, 10, 2, 3), True),        # containment
            ((3, 4, 3, 4), True),         # identical
        ]
        for (a1, b1, a2, b2), expected in cases:
            with self.subTest(case=(a1, b1, a2, b2)):
                self.assertEqual(overlaps(a1, b1, a2, b2), expected)
                self.assertEqual(overlaps(a2, b2, a1, b1), expected)

    def test_contains_is_closed_open(self):
        self.assertTrue(contains(10, 20, 10))
        self.assertFalse(contains(10, 20, 20))
        self.assertTrue(contains(10, None, 10 ** 18))
        self.assertFalse(contains(10, None, 9))

    def test_invalid_interval(self):
        for vf, vt in ((10, 10), (10, 9)):
            with self.assertRaises(RecallError) as raised:
                check_interval(vf, vt)
            self.assertEqual(raised.exception.code, "invalid_input")
        check_interval(10, 11)
        check_interval(10, None)

    def test_back_dated_default_valid_to(self):
        history = [A(1, 100, acc=1), A(2, 200, acc=2), A(3, 50, state="candidate"), R(4, 300, acc=4)]
        self.assertEqual(default_valid_to(150, history), 200)   # next later-starting accepted assert
        self.assertEqual(default_valid_to(10, history), 100)    # candidates and retractions ignored
        self.assertEqual(default_valid_to(100, history), 200)   # strictly greater
        self.assertIsNone(default_valid_to(250, history))

    def test_retraction_default_interval_is_effective_interval(self):
        history = [A(1, 100, acc=1), A(2, 200, acc=2), A(3, 150, 400, acc=3)]
        self.assertEqual(effective_interval(history[0], history), (100, 150))
        self.assertEqual(effective_interval(history[1], history), (200, None))
        self.assertEqual(effective_interval(history[2], history), (150, 200))
        bounded = [A(1, 100, 120, acc=1), A(2, 200, acc=2)]
        self.assertEqual(effective_interval(bounded[0], bounded), (100, 120))

    def test_conflict_versus_forward_change(self):
        existing = A(1, 100, acc=1)
        history = [existing]
        self.assertTrue(conflicts_with(100, None, '"b"', existing, '"a"', history))   # same start, differs
        self.assertTrue(conflicts_with(50, 150, '"b"', existing, '"a"', history))     # back-dated overlap
        self.assertFalse(conflicts_with(50, 100, '"b"', existing, '"a"', history))    # touching
        self.assertFalse(conflicts_with(150, None, '"b"', existing, '"a"', history))  # forward change
        self.assertFalse(conflicts_with(100, None, '"a"', existing, '"a"', history))  # same value


class CurrentTruthTests(unittest.TestCase):
    def test_empty_and_single(self):
        self.assertIsNone(current_fact([], 5, 5))
        rev = A(1, 100, acc=10)
        self.assertEqual(current_fact([rev], 100, 10), rev)
        self.assertIsNone(current_fact([rev], 99, 10))   # before valid_from
        self.assertIsNone(current_fact([rev], 100, 9))   # not yet known

    def test_expiry_boundary_valid_to_equal_is_not_current(self):
        rev = A(1, 100, 200, acc=10)
        self.assertEqual(current_fact([rev], 199, 10), rev)
        self.assertIsNone(current_fact([rev], 200, 10))
        other = Revision(1, 100, 200, "accepted", 10)
        self.assertEqual(current_other([other], 199, 10), other)
        self.assertIsNone(current_other([other], 200, 10))

    def test_later_acceptance_wins(self):
        old, new = A(1, 100, acc=10), A(2, 150, acc=20)
        self.assertEqual(current_fact([old, new], 160, 30), new)
        self.assertEqual(current_fact([old, new], 120, 30), old)    # new does not cover 120
        self.assertEqual(current_fact([old, new], 160, 15), old)    # new not yet known

    def test_rev_breaks_accepted_at_ties(self):
        a, b = A(1, 100, acc=10), A(2, 100, acc=10)
        self.assertEqual(current_fact([b, a], 100, 10), b)

    def test_back_dated_value_does_not_override_later_value(self):
        later = A(1, 200, acc=10)
        back = A(2, 100, 200, acc=20)   # default valid_to = 200
        self.assertEqual(current_fact([later, back], 150, 30), back)
        self.assertEqual(current_fact([later, back], 250, 30), later)

    def test_retraction(self):
        rev = A(1, 100, acc=10)
        retraction = R(2, 100, None, acc=20)
        self.assertIsNone(current_fact([rev, retraction], 150, 30))
        self.assertEqual(current_fact([rev, retraction], 150, 15), rev)   # before the retraction was known
        later = A(3, 300, acc=30)
        self.assertEqual(current_fact([rev, R(2, 100, 300, acc=20), later], 350, 40), later)

    def test_candidates_and_rejected_never_current(self):
        self.assertIsNone(current_fact([A(1, 100, state="candidate")], 150, 10 ** 9))
        self.assertIsNone(current_fact([A(1, 100, state="rejected")], 150, 10 ** 9))


class StoreCurrentTruthTests(StoreTestCase):
    """The same rules through the SQL store."""

    def setUp(self):
        self.store = self.new_store()
        self.alice = self.add_principal(self.store, "alice")

    def save(self, value, request_id, **kwargs):
        return self.store.save(self.alice, kind="fact", request_id=request_id,
                               fact={"subject": "Service X", "predicate": "owner", "value": value}, **kwargs)

    def current(self, tv, tk=None):
        rows = self.store.lookup_facts(self.alice, subject="service x", predicate="OWNER", as_of=tv,
                                       known_at=tk if tk is not None else self.clock.now + 10 ** 9)
        self.assertLessEqual(len(rows), 1)
        return rows[0]["fact"]["value"] if rows else None

    def test_supersession_back_dating_retraction_and_expiry(self):
        first = self.save("ann", "r1", valid_from=T0 - 1000)
        self.assertEqual(first["rev"], 1)
        second = self.save("bob", "r2", valid_from=T0 + 500_000)
        self.assertEqual(self.current(T0 + 100), "ann")
        self.assertEqual(self.current(T0 + 600_000), "bob")
        history = self.store.history(self.alice, item_id=first["item_id"])
        self.assertEqual(history[1]["fact"]["supersedes"], 1)
        self.assertIsNone(history[1]["valid_to"])

        # Back-dated: valid_to defaults to the next later-starting accepted assert.
        back = self.save("carol", "r3", valid_from=T0 + 200_000)
        revision = self.store.get(self.alice, item_id=first["item_id"], rev=back["rev"])
        self.assertEqual(revision["valid_to"], history[1]["valid_from"])
        self.assertEqual(self.current(T0 + 300_000), "carol")
        self.assertEqual(self.current(T0 + 600_000), "bob")   # the back-dated value did not override

        # Retraction defaults to the effective interval [T0+200000, T0+500000) and never hides the later
        # value. Inside that interval it is the latest accepted covering revision, so no value is current.
        retraction = self.store.retract(self.alice, item_id=first["item_id"], rev=back["rev"], request_id="r4")
        retracted = self.store.history(self.alice, item_id=first["item_id"])[retraction["rev"] - 1]
        self.assertEqual((retracted["valid_from"], retracted["valid_to"]), (revision["valid_from"], revision["valid_to"]))
        self.assertEqual(retracted["fact"]["op"], "retract")
        self.assertIsNone(self.current(T0 + 300_000))
        self.assertEqual(self.current(T0 + 100), "ann")
        self.assertEqual(self.current(T0 + 600_000), "bob")

        # Expiry boundary: valid_to exactly equal to the query time is not current.
        end = T0 + 900_000
        self.save("dave", "r5", valid_from=T0 + 800_000, valid_to=end)
        self.assertEqual(self.current(end - 1), "dave")
        self.assertEqual(self.current(end), "bob")   # dave no longer covers; bob is the latest covering
        self.assertEqual(second["rev"], 2)

    def test_known_time(self):
        self.save("ann", "r1", valid_from=T0 - 10)
        known_before = self.clock.now - 1   # the next save is accepted at clock.now
        self.save("bob", "r2", valid_from=T0 - 5)
        self.assertEqual(self.current(T0 + 10 ** 6, known_before), "ann")
        self.assertEqual(self.current(T0 + 10 ** 6), "bob")

    def test_invalid_interval_rejected(self):
        self.assertCode("invalid_input", self.save, "x", "r1", valid_from=T0, valid_to=T0)
        self.assertCode("invalid_input", self.save, "x", "r2", valid_from="2026-11-25T09:30:00", valid_to=None)
        # A back-dated default that would be empty cannot happen: default valid_to is strictly later.
        self.save("a", "r3", valid_from=T0 + 100)
        out = self.save("b", "r4", valid_from=T0 + 99)
        self.assertEqual(self.store.get(self.alice, item_id=out["item_id"], rev=out["rev"])["valid_to"],
                         self.store.get(self.alice, item_id=out["item_id"], rev=1)["valid_from"])

    def test_non_fact_expiry(self):
        out = self.store.save(self.alice, kind="note", request_id="n1", key="note-1", body="hello",
                              valid_to=T0 + 50_000)
        self.assertEqual(self.store.get(self.alice, item_id=out["item_id"], as_of=T0 + 49_999)["rev"], 1)
        self.assertCode("not_found", self.store.get, self.alice, item_id=out["item_id"], as_of=T0 + 50_000)


if __name__ == "__main__":
    unittest.main()
