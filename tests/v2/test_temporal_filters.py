"""as_of (valid time) and known_at (known time) in lookup: explicit cases."""
from __future__ import annotations

import unittest

from recall_v2.core.timefmt import format_rfc3339
from recall_v2.retrieval import lookup

from .helpers import T0
from .retrieval_helpers import RetrievalTestCase

DAY = 86_400 * 1_000_000


class TemporalTests(RetrievalTestCase):
    def setUp(self):
        self.store = self.open_store()
        self.a = self.add_principal(self.store, "alpha")

    def value(self, as_of, known_at, mode="fact"):
        if mode == "fact":
            env = lookup(self.store, self.a, subject="svc", predicate="owner", as_of=as_of, known_at=known_at)
        else:
            env = lookup(self.store, self.a, query="svc owner", as_of=as_of, known_at=known_at)
        self.assertTrue(env["ok"], env)
        self.assertLessEqual(env["count"], 1)
        return env["results"][0]["fact"]["value"] if env["results"] else None

    def at(self, when):
        self.clock.now = when

    def test_change_back_date_and_retraction(self):
        # Day 10: v1 valid from day 0. Day 20: v2 valid from day 15. Day 30: back-dated v0 from day -5.
        # Day 40: retract v2.
        self.at(T0 + 10 * DAY)
        first = self.fact(self.store, self.a, "svc", "owner", "v1", valid_from=T0)
        self.at(T0 + 20 * DAY)
        self.fact(self.store, self.a, "svc", "owner", "v2", valid_from=T0 + 15 * DAY)
        self.at(T0 + 30 * DAY)
        self.fact(self.store, self.a, "svc", "owner", "v0", valid_from=T0 - 5 * DAY)
        self.at(T0 + 40 * DAY)
        self.store.retract(self.a, item_id=first["item_id"], rev=2, request_id="retract-v2")
        self.at(T0 + 50 * DAY)

        for mode in ("fact", "text"):
            with self.subTest(mode=mode):
                v = lambda tv, tk: self.value(T0 + tv * DAY, T0 + tk * DAY, mode)  # noqa: E731
                self.assertIsNone(v(-10, 50))      # before every interval
                self.assertIsNone(v(5, 5))         # nothing known yet
                self.assertEqual(v(5, 12), "v1")
                self.assertEqual(v(16, 12), "v1")  # v2 not yet known: v1 is open-ended then
                self.assertEqual(v(16, 25), "v2")
                self.assertIsNone(v(-3, 25))       # back-dated value not yet known
                self.assertEqual(v(-3, 35), "v0")  # ... and bounded by v1's start
                self.assertEqual(v(1, 35), "v1")
                self.assertEqual(v(16, 35), "v2")
                self.assertIsNone(v(16, 45))       # v2 retracted
                self.assertEqual(v(5, 45), "v1")   # retraction does not hide v1
                # default as_of / known_at = now (day 50)
                self.assertIsNone(self.value(None, None, mode))

    def test_expiry_and_string_timestamps(self):
        self.at(T0)
        self.fact(self.store, self.a, "svc", "owner", "temp", valid_from=T0, valid_to=T0 + 7 * DAY)
        self.at(T0 + 10 * DAY)
        self.assertIsNone(self.value(None, None))
        self.assertEqual(self.value(format_rfc3339(T0 + 3 * DAY), None), "temp")
        self.assertIsNone(self.value(format_rfc3339(T0 + 7 * DAY), None))  # closed-open interval
        env = lookup(self.store, self.a, subject="svc", as_of=format_rfc3339(T0), known_at=format_rfc3339(T0))
        self.assertEqual((env["as_of"], env["known_at"]), (format_rfc3339(T0), format_rfc3339(T0)))

    def test_non_fact_known_at_and_as_of(self):
        self.at(T0)
        self.store.save(self.a, kind="note", request_id="n1", key="plan", title="plan", body="first plan")
        self.at(T0 + DAY)
        self.store.save(self.a, kind="note", request_id="n2", key="plan", title="plan", body="second plan",
                        valid_to=T0 + 3 * DAY)
        self.at(T0 + 5 * DAY)
        bodies = lambda **kw: [r["body"] for r in lookup(self.store, self.a, query="plan", **kw)["results"]]  # noqa
        self.assertEqual(bodies(as_of=T0 + 2 * DAY, known_at=T0 + 2 * DAY), ["second plan"])
        self.assertEqual(bodies(), [])  # latest revision expired at day 3; older revisions do not resurface
        self.assertEqual(bodies(as_of=T0 + DAY // 2, known_at=T0 + DAY // 2), [])  # FTS holds the current revision


if __name__ == "__main__":
    unittest.main()
