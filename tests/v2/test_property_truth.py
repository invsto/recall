"""Property test: seeded random set / supersede / back-date / retract / expire sequences against an
independent in-memory reference of spec 2.4 and 2.5. Uses stdlib random (no extra dependency)."""
from __future__ import annotations

import random
import unittest

from recall_v2.core import validity
from recall_v2.core.errors import RecallError

from .helpers import StoreTestCase, StepClock, T0

SEED = 20261125
INF = float("inf")


class Reference:
    """Brute-force model of one fact key. Written from the spec text, not from recall_v2.core."""

    def __init__(self):
        self.revs: list[dict] = []

    def asserts(self):
        return [r for r in self.revs if r["op"] == "assert"]

    def add_assert(self, rev, vf, vt, acc, value):
        if vt is None:
            later = [r["vf"] for r in self.asserts() if r["vf"] > vf]
            vt = min(later) if later else None
        self.revs.append({"rev": rev, "op": "assert", "vf": vf, "vt": vt, "acc": acc, "value": value})
        return vt

    def add_retract(self, rev, target_rev, vf, vt, acc):
        target = next(r for r in self.revs if r["rev"] == target_rev)
        ends = [r["vf"] for r in self.asserts() if r["vf"] > target["vf"]]
        if target["vt"] is not None:
            ends.append(target["vt"])
        start = target["vf"] if vf is None else vf
        end = (min(ends) if ends else None) if vt is None else vt
        if end is not None and end <= start:
            return False
        self.revs.append({"rev": rev, "op": "retract", "vf": start, "vt": end, "acc": acc, "value": target["value"]})
        return True

    def current(self, tv, tk):
        best = None
        for r in self.revs:
            end = INF if r["vt"] is None else r["vt"]
            if r["acc"] <= tk and r["vf"] <= tv < end:
                if best is None or (r["acc"], r["rev"]) > (best["acc"], best["rev"]):
                    best = r
        return None if best is None or best["op"] == "retract" else best


SUBJECT_SPELLINGS = {
    0: ("Service A", "Service  a", "SERVICE A"),
    1: ("Service B", "service b", " Service B "),
    2: ("Ｓｅｒｖｉｃｅ C", "service c", "SERVICE C"),
}


class PropertyTruthTests(StoreTestCase):
    def run_sequence(self, seed: int, steps: int):
        rng = random.Random(seed)
        store = self.new_store(StepClock(T0, rng.randint(1, 5) * 1_000))
        alice = self.add_principal(store, "alice")
        models = {k: Reference() for k in SUBJECT_SPELLINGS}
        item_ids: dict[int, str] = {}
        boundaries = {T0}
        request = 0

        for _ in range(steps):
            key = rng.choice(list(SUBJECT_SPELLINGS))
            model = models[key]
            subject = rng.choice(SUBJECT_SPELLINGS[key])
            request += 1
            accepted_at = self.clock.now          # the store reads the clock once per write
            next_rev = len(model.revs) + 1
            accepted_asserts = model.asserts()
            action = rng.random()
            if action < 0.30 and accepted_asserts and key in item_ids:
                target = rng.choice(accepted_asserts)["rev"]
                vf = vt = None
                if rng.random() < 0.3:
                    vf = next(r for r in model.revs if r["rev"] == target)["vf"]
                    vt = vf + rng.randint(1, 400_000)
                trial = Reference()
                trial.revs = [dict(r) for r in model.revs]
                expect_ok = trial.add_retract(next_rev, target, vf, vt, accepted_at)
                try:
                    out = store.retract(alice, item_id=item_ids[key], rev=target, request_id=f"q{request}",
                                        valid_from=vf, valid_to=vt)
                except RecallError as exc:
                    self.assertFalse(expect_ok, exc.message)
                    self.assertEqual(exc.code, "invalid_input")
                    continue
                self.assertTrue(expect_ok)
                self.assertEqual(out["rev"], next_rev)
                self.stats["retract"] += 1
                model.add_retract(next_rev, target, vf, vt, accepted_at)
            else:
                # set now / back-dated / future-dated, sometimes with an explicit expiry
                mode = rng.random()
                if mode < 0.4:
                    vf = None
                elif mode < 0.8:
                    vf = accepted_at - rng.randint(1, 600_000)
                else:
                    vf = accepted_at + rng.randint(1, 600_000)
                vt = None
                if rng.random() < 0.2:
                    base = accepted_at if vf is None else vf
                    vt = base + rng.randint(-50, 300_000)
                value = rng.choice(["ann", "bob", "carol", {"n": rng.randint(0, 3)}])
                effective_vf = accepted_at if vf is None else vf
                try:
                    out = store.save(alice, kind="fact", request_id=f"q{request}", valid_from=vf, valid_to=vt,
                                     fact={"subject": subject, "predicate": "Owner", "value": value})
                except RecallError as exc:
                    self.assertEqual(exc.code, "invalid_input")
                    self.assertIsNotNone(vt)
                    self.assertLessEqual(vt, effective_vf)
                    continue
                self.assertEqual(out["rev"], next_rev, "one item per fact key, rev increments")
                item_ids.setdefault(key, out["item_id"])
                self.assertEqual(out["item_id"], item_ids[key])
                stored_vt = model.add_assert(next_rev, effective_vf, vt, accepted_at, value)
                self.stats["back_dated" if effective_vf < accepted_at else "set"] += 1
                if vt is not None:
                    self.stats["expiry"] += 1
                elif stored_vt is not None:
                    self.stats["default_valid_to"] += 1
                got = store.get(alice, item_id=out["item_id"], rev=out["rev"])
                self.assertEqual(got["valid_to"], None if stored_vt is None else _fmt(stored_vt))
            for r in model.revs:
                boundaries.update(x for x in (r["vf"], r["vt"]) if x is not None)

            self.check_points(rng, store, alice, models, item_ids, boundaries)

    def check_points(self, rng, store, alice, models, item_ids, boundaries):
        accepted = sorted({r["acc"] for m in models.values() for r in m.revs}) or [T0]
        points = sorted(boundaries)
        for _ in range(6):
            tv = rng.choice(points) + rng.choice((-1, 0, 0, 1, rng.randint(-100_000, 100_000)))
            tk = rng.choice(accepted) + rng.choice((-1, 0, 1, 10 ** 9))
            visible_now = store.lookup_facts(alice, predicate="owner", as_of=tv, known_at=tk)
            keys = [row["id"] for row in visible_now]
            self.assertEqual(len(keys), len(set(keys)), "at most one current revision per key")
            by_item = {row["id"]: row for row in visible_now}
            for key, model in models.items():
                expected = model.current(tv, tk)
                core = validity.current_fact(
                    [validity.FactRevision(r["rev"], r["op"], r["vf"], r["vt"], "accepted", r["acc"])
                     for r in model.revs], tv, tk)
                self.assertEqual(None if core is None else core.rev, None if expected is None else expected["rev"])
                item_id = item_ids.get(key)
                row = by_item.get(item_id) if item_id else None
                self.stats["current" if expected is not None else "none"] += 1
                self.assertEqual(None if row is None else row["rev"],
                                 None if expected is None else expected["rev"], (key, tv, tk))
                if item_id is not None:
                    try:
                        got = store.get(alice, item_id=item_id, as_of=tv, known_at=tk)["rev"]
                    except RecallError as exc:
                        self.assertEqual(exc.code, "not_found")
                        got = None
                    self.assertEqual(got, None if expected is None else expected["rev"])

    def test_random_histories_match_reference(self):
        from collections import Counter
        self.stats = Counter()
        for offset in range(12):
            with self.subTest(seed=SEED + offset):
                self.run_sequence(SEED + offset, steps=45)
        # The generator must actually exercise every operation and both outcomes.
        for name in ("set", "back_dated", "retract", "expiry", "default_valid_to", "current", "none"):
            self.assertGreater(self.stats[name], 10, (name, dict(self.stats)))


def _fmt(micros):
    from recall_v2.core.timefmt import format_rfc3339
    return format_rfc3339(micros)


class CorePropertyTests(unittest.TestCase):
    """Pure core rule against the reference on many small random histories."""

    def test_core_current_fact_matches_reference(self):
        rng = random.Random(SEED)
        for _ in range(3000):
            ref = Reference()
            acc = 0
            for rev in range(1, rng.randint(1, 7)):
                acc += rng.randint(0, 2)  # allow accepted_at ties (rev breaks them)
                if ref.asserts() and rng.random() < 0.3:
                    target = rng.choice(ref.asserts())["rev"]
                    if not ref.add_retract(rev, target, None, None, acc):
                        break
                else:
                    vf = rng.randint(0, 20)
                    vt = rng.choice([None, None, vf + rng.randint(1, 10)])
                    ref.add_assert(rev, vf, vt, acc, rev)
            revisions = [validity.FactRevision(r["rev"], r["op"], r["vf"], r["vt"], "accepted", r["acc"])
                         for r in ref.revs]
            for tv in range(-1, 32):
                for tk in range(-1, acc + 2):
                    want = ref.current(tv, tk)
                    got = validity.current_fact(revisions, tv, tk)
                    self.assertEqual(None if got is None else got.rev, None if want is None else want["rev"])


if __name__ == "__main__":
    unittest.main()
