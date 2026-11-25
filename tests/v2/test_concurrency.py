"""Parallel writers on separate connections to one file (spec 7.2 transactions, M3 concurrency)."""
from __future__ import annotations

import threading
import unittest

from recall_v2.api import Client, Operator

from .governance_helpers import GovernanceCase

WRITERS = 8


class LockedClock:
    def __init__(self, start: int):
        self.now = start
        self.lock = threading.Lock()

    def __call__(self) -> int:
        with self.lock:
            self.now += 1_000
            return self.now


class ConcurrencyTests(GovernanceCase):
    def race(self, work, count=WRITERS):
        """Run work(n, client_or_operator_factory) in `count` threads, each with its own connection."""
        clock = LockedClock(self.clock.now)
        barrier = threading.Barrier(count)
        results: list = [None] * count
        errors: list = []

        def run(n):
            try:
                results[n] = work(n, clock, barrier)
            except BaseException as exc:   # surfaced below
                errors.append(exc)
                barrier.abort()

        threads = [threading.Thread(target=run, args=(n,)) for n in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        return results

    def client(self, name, clock):
        return Client.open(self.path, self.tokens[name], clock=clock)

    def test_cas_race_on_one_fact_key_has_exactly_one_winner(self):
        def work(n, clock, barrier):
            with self.client("alice", clock) as client:
                barrier.wait()
                return client.save(kind="fact", request_id=f"w{n}", expected_revision=0,
                                   fact={"subject": "svc", "predicate": "owner", "value": f"writer-{n}"})
        envelopes = self.race(work)
        winners = [e for e in envelopes if e["ok"]]
        losers = [e for e in envelopes if not e["ok"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["results"][0]["rev"], 1)
        self.assertEqual({(e["error"]["code"], e["error"]["details"]["current_revision"]) for e in losers},
                         {("conflict", 1)})
        self.assertEqual(self.count("item_revisions"), 1)
        self.assertEqual(self.count("receipts"), 1)

    def test_parallel_saves_without_cas_lose_nothing_and_never_share_a_rev(self):
        def work(n, clock, barrier):
            with self.client("alice", clock) as client:
                barrier.wait()
                return client.save(kind="fact", request_id=f"w{n}",
                                   fact={"subject": "svc", "predicate": "owner", "value": f"writer-{n}"})
        envelopes = self.race(work)
        self.assertTrue(all(e["ok"] for e in envelopes), envelopes)
        revs = sorted(e["results"][0]["rev"] for e in envelopes)
        self.assertEqual(revs, list(range(1, WRITERS + 1)))
        self.assertEqual(len({e["results"][0]["id"] for e in envelopes}), 1)
        values = {r[0] for r in self.store.db.execute("SELECT value_json FROM fact_versions")}
        self.assertEqual(len(values), WRITERS)
        self.assertEqual(self.store.db.execute("SELECT current_rev FROM items").fetchone()[0], WRITERS)

    def test_parallel_project_candidates_get_distinct_revs(self):
        def work(n, clock, barrier):
            with self.client("alice" if n % 2 else "bob", clock) as client:
                barrier.wait()
                return client.save(kind="note", request_id=f"w{n}", scope="project", project="proj", key="plan",
                                   body=f"version {n}")
        envelopes = self.race(work)
        self.assertTrue(all(e["ok"] for e in envelopes), envelopes)
        self.assertEqual(sorted(e["results"][0]["rev"] for e in envelopes), list(range(1, WRITERS + 1)))

    def test_racing_approvals_of_competing_candidates_have_one_winner(self):
        a = self.project_note(self.alice, "plan", "alice version")
        b = self.project_note(self.bob, "plan", "bob version")

        def work(n, clock, barrier):
            with Operator.open(self.path, clock=clock) as op:
                barrier.wait()
                return op.review_approve(a["id"], (a["rev"], b["rev"])[n])
        envelopes = self.race(work, count=2)
        self.assertEqual(sorted(e["ok"] for e in envelopes), [False, True])
        loser = next(e for e in envelopes if not e["ok"])
        self.assertEqual(loser["error"]["code"], "stale_review")
        self.assertEqual(self.count("item_revisions", "state = 'accepted'"), 1)

    def test_edit_racing_an_approval(self):
        a = self.project_note(self.alice, "plan", "alice version")
        b = self.project_note(self.bob, "plan", "bob version")

        def work(n, clock, barrier):
            with Operator.open(self.path, clock=clock) as op:
                barrier.wait()
                if n == 0:
                    return op.review_edit(b["id"], b["rev"], body="bob, edited")
                return op.review_approve(a["id"], a["rev"])
        envelopes = self.race(work, count=2)
        self.assertEqual(sorted(e["ok"] for e in envelopes), [False, True])
        self.assertEqual(next(e for e in envelopes if not e["ok"])["error"]["code"], "stale_review")
        self.assertEqual(self.count("item_revisions", "state = 'accepted'"), 1)


if __name__ == "__main__":
    unittest.main()
