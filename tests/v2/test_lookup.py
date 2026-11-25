"""Lookup: fact mode, text mode, arguments, scope in every leg, deletion and expiry, FTS canary."""
from __future__ import annotations

from fractions import Fraction
import math
import unittest

from recall_v2.retrieval import canonical_bytes, lookup
from recall_v2.retrieval import fusion, lexical
from recall_v2.store import maintenance

from .retrieval_helpers import FakeEmbedder, RetrievalTestCase


class FactModeTests(RetrievalTestCase):
    def test_exact_normalised_match_and_order(self):
        store = self.open_store()
        a = self.add_principal(store, "alpha")
        store.project_add("p1")
        store.member_add("alpha", "p1")
        self.fact(store, a, "Service-X", "Owner", "team-a")
        self.fact(store, a, "service-x", "region", "eu")
        self.fact(store, a, "other", "owner", "team-b")
        project = store.save(a, kind="fact", request_id="pf", scope="project", project="p1",
                             fact={"subject": "service-x", "predicate": "owner", "value": "team-p"})
        store.review_approve(project["item_id"], project["rev"])

        env = lookup(store, a, subject="  SERVICE-x ", predicate="owner")
        self.assertTrue(env["ok"])
        self.assertEqual([(r["scope"]["type"], r["fact"]["value"]) for r in env["results"]],
                         [("private", "team-a"), ("project", "team-p")])
        self.assertTrue(all(r["rank"] is None and r["score"] is None for r in env["results"]))
        self.assertTrue(all(r["trust"] == "untrusted" for r in env["results"]))

        env = lookup(store, a, subject="service-x")
        self.assertEqual([r["fact"]["predicate"] for r in env["results"]], ["Owner", "region", "owner"])
        env = lookup(store, a, predicate="OWNER")
        self.assertEqual([r["fact"]["value"] for r in env["results"]], ["team-b", "team-a", "team-p"])
        env = lookup(store, a, predicate="owner", scope="project")
        self.assertEqual([r["fact"]["value"] for r in env["results"]], ["team-p"])
        env = lookup(store, a, subject="service-x", kind="note")
        self.assertEqual(env["results"], [])

    def test_limit_and_truncated(self):
        store = self.open_store()
        a = self.add_principal(store, "alpha")
        for n in range(25):
            self.fact(store, a, "host", f"attr{n:02d}", n)
        env = lookup(store, a, subject="host")
        self.assertEqual((env["count"], env["truncated"]), (10, True))
        env = lookup(store, a, subject="host", limit=20)
        self.assertEqual((env["count"], env["truncated"]), (20, True))
        env = lookup(store, a, subject="host", predicate="attr03")
        self.assertEqual((env["count"], env["truncated"]), (1, False))

    def test_invalid_arguments(self):
        store = self.open_store()
        a = self.add_principal(store, "alpha")
        cases = [dict(), dict(query="x", subject="y"), dict(query=""), dict(query="x" * 257),
                 dict(query="x", limit=0), dict(query="x", limit=21), dict(query="x", limit=True),
                 dict(query="x", kind="memo"), dict(query="x", scope="team"),
                 dict(query="x", scope="private", project="p1"), dict(subject="\x07"),
                 dict(query="x", as_of="2026-11-25T09:30:00"), dict(query="x", explain="yes")]
        for kwargs in cases:
            env = lookup(store, a, **kwargs)
            self.assertFalse(env["ok"], kwargs)
            self.assertEqual(env["error"]["code"], "invalid_input", kwargs)
        self.assertTrue(lookup(store, a, query="é" * 128)["ok"])  # exactly 256 bytes

    def test_non_member_project_is_not_found(self):
        store = self.open_store()
        a = self.add_principal(store, "alpha")
        store.project_add("p1")
        for project in ("p1", "missing"):
            env = lookup(store, a, query="x", project=project)
            self.assertEqual(env["error"]["code"], "not_found")


class TextModeTests(RetrievalTestCase):
    def test_title_weight_and_fts_syntax_is_inert(self):
        store = self.open_store()
        a = self.add_principal(store, "alpha")
        body_hit = self.note(store, a, "n1", "Unrelated", "the retry policy lives here")["item_id"]
        title_hit = self.note(store, a, "n2", "Retry policy", "three attempts")["item_id"]
        env = lookup(store, a, query="retry")
        self.assertEqual([r["id"] for r in env["results"]], [title_hit, body_hit])
        for hostile in ('retry" OR body:*', "NEAR(retry policy)", "retry*", "title:retry", "(", '"'):
            env = lookup(store, a, query=hostile)
            self.assertTrue(env["ok"], hostile)
        self.assertEqual(lookup(store, a, query="***")["results"], [])

    def test_facts_match_on_current_revision_only(self):
        store = self.open_store()
        a = self.add_principal(store, "alpha")
        self.fact(store, a, "db", "engine", "postgresql")
        self.fact(store, a, "db", "engine", "sqlite")
        self.assertEqual(lookup(store, a, query="postgresql")["results"], [])
        [hit] = lookup(store, a, query="sqlite")["results"]
        self.assertEqual((hit["rev"], hit["fact"]["value"]), (2, "sqlite"))
        [hit] = lookup(store, a, query="engine")["results"]
        self.assertEqual(hit["rev"], 2)

    def test_bm25_statistics_use_the_visible_corpus(self):
        """Hand-computed BM25 for a three-document visible corpus; another principal's documents do not count."""
        store = self.open_store()
        a = self.add_principal(store, "alpha")
        b = self.add_principal(store, "beta")
        one = self.note(store, a, "n1", "alpha beta", "gamma")["item_id"]
        self.note(store, a, "n2", "delta", "epsilon zeta")
        self.note(store, a, "n3", "eta", "theta iota kappa")
        for n in range(5):
            self.note(store, b, f"b{n}", "beta beta", "beta words that are long and many")
        env = lookup(store, a, query="beta", explain=True)
        [hit] = env["results"]
        self.assertEqual(hit["id"], one)
        n, df, dl, avgdl, w = 3, 1, 3, 10 / 3, 5.0
        idf = math.log((n - df + 0.5) / (df + 0.5))
        expected = idf * w * 2.2 / (w + 1.2 * (1 - 0.75 + 0.75 * dl / avgdl))
        self.assertAlmostEqual(hit["explain"]["lexical"]["score"], expected, places=12)


class ScopeTests(RetrievalTestCase):
    def build(self, embedder=None):
        store = self.open_store(embedder=embedder)
        a = self.add_principal(store, "alpha")
        b = self.add_principal(store, "beta")
        store.project_add("p1")
        store.member_add("beta", "p1")
        self.note(store, a, "mine", "kraken notes", "kraken deployment runbook")
        self.note(store, a, "mine2", "other", "kraken once")
        self.project_note(store, b, "p1", "proj", "kraken project", "kraken in the project")
        self.note(store, b, "private-b", "kraken private beta", "kraken kraken kraken")
        return store, a, b

    def test_non_member_sees_no_project_items_in_any_leg(self):
        store, a, b = self.build(FakeEmbedder())
        env = lookup(store, a, query="kraken project", explain=True)
        self.assertEqual({r["scope"]["id"] for r in env["results"]}, {"alpha"})
        self.assertIn("vector", env["results"][0]["explain"])
        self.assertEqual(lookup(store, a, query="kraken", scope="project")["results"], [])
        env = lookup(store, b, query="kraken", explain=True)
        self.assertEqual({r["scope"]["id"] for r in env["results"]}, {"beta", "p1"})
        store.member_add("alpha", "p1")
        self.assertIn("p1", {r["scope"]["id"] for r in lookup(store, a, query="kraken")["results"]})
        store.member_remove("alpha", "p1")
        self.assertNotIn("p1", {r["scope"]["id"] for r in lookup(store, a, query="kraken")["results"]})

    def test_others_data_never_affects_results_counts_or_scores(self):
        for embedder in (None, FakeEmbedder()):
            with self.subTest(vector=embedder is not None):
                store, a, b = self.build(embedder)
                args = [dict(query="kraken", explain=True, as_of=self.clock.now, known_at=self.clock.now),
                        dict(query="kraken runbook", explain=True, as_of=self.clock.now, known_at=self.clock.now,
                             limit=1),
                        dict(subject="svc", as_of=self.clock.now, known_at=self.clock.now)]
                before = [canonical_bytes(lookup(store, a, **kw)) for kw in args]
                for n in range(30):
                    self.note(store, b, f"flood{n}", "kraken kraken", "kraken runbook deployment " * 3)
                    self.project_note(store, b, "p1", f"pflood{n}", "kraken", "kraken runbook")
                store.save(b, kind="fact", request_id="bf", fact={"subject": "svc", "predicate": "x", "value": 1})
                after = [canonical_bytes(lookup(store, a, **kw)) for kw in args]
                self.assertEqual(before, after)

    def test_deleted_and_expired_items_never_appear(self):
        store = self.open_store(embedder=FakeEmbedder())
        a = self.add_principal(store, "alpha")
        gone = self.note(store, a, "gone", "walrus", "walrus facts")["item_id"]
        start = self.clock.now
        expiring = self.note(store, a, "exp", "walrus expiring", "walrus", valid_to=start + 10_000)["item_id"]
        fact = self.fact(store, a, "walrus", "count", 3)["item_id"]
        store.delete_item(gone)
        store.delete_item(fact)
        self.clock.now = start + 20_000
        for kwargs in (dict(query="walrus"), dict(subject="walrus")):
            ids = [r["id"] for r in lookup(store, a, **kwargs)["results"]]
            self.assertNotIn(gone, ids)
            self.assertNotIn(fact, ids)
            self.assertNotIn(expiring, ids)
        ids = [r["id"] for r in lookup(store, a, query="walrus", as_of=start + 5_000)["results"]]
        self.assertEqual(ids, [expiring])

    def test_deleted_term_canary(self):
        store = self.open_store(embedder=FakeEmbedder())
        a = self.add_principal(store, "alpha")
        canary = "zqxcanaryterm"
        note = self.note(store, a, "secret", f"title {canary}", f"body with {canary} inside")["item_id"]
        fact = self.fact(store, a, "vault", "code", f"{canary}-fact")["item_id"]
        self.assertEqual(len(lookup(store, a, query=canary)["results"]), 2)
        store.delete_item(note)
        store.delete_item(fact)
        db = store.db
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fts_items WHERE fts_items MATCH ?", (canary,)).fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fts_items_vocab WHERE term = ?", (canary,)).fetchone()[0], 0)
        self.assertEqual(lookup(store, a, query=canary)["results"], [])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 0)
        maintenance.compact(db)
        self.assertNotIn(canary.encode(), self.path.read_bytes())
        wal = self.path.with_name(self.path.name + "-wal")
        if wal.exists():
            self.assertNotIn(canary.encode(), wal.read_bytes())


class FusionTests(unittest.TestCase):
    def test_hand_computed_rrf(self):
        legs = {"lexical": [("A", 5), ("B", 5), ("C", 5)], "vector": [("C", 5), ("A", 5), ("D", 5)]}
        fused = fusion.rrf(legs)
        self.assertEqual([f.item_id for f in fused], ["A", "C", "B", "D"])
        self.assertEqual(fused[0].score, Fraction(1, 61) + Fraction(1, 62))
        self.assertEqual(fused[1].score, Fraction(1, 63) + Fraction(1, 61))
        self.assertEqual(fused[2].score, Fraction(1, 62))
        self.assertEqual(fused[3].score, Fraction(1, 63))
        self.assertAlmostEqual(float(fused[0].score), 0.032522474881015334, places=15)

    def test_ties_accepted_at_desc_then_id(self):
        legs = {"lexical": [("B", 1), ("Z", 9)], "vector": [("A", 1), ("Y", 9)]}
        self.assertEqual([f.item_id for f in fusion.rrf(legs)], ["A", "B", "Y", "Z"])
        legs = {"lexical": [("B", 9)], "vector": [("A", 1)]}
        self.assertEqual([f.item_id for f in fusion.rrf(legs)], ["B", "A"])

    def test_one_leg_keeps_its_order(self):
        ranked = [("K", 1), ("A", 9), ("M", 3)]
        self.assertEqual([f.item_id for f in fusion.rrf({"lexical": ranked})], ["K", "A", "M"])


class ExplainTests(RetrievalTestCase):
    def test_contributions_match_rrf(self):
        store = self.open_store(embedder=FakeEmbedder())
        a = self.add_principal(store, "alpha")
        for n, (title, body) in enumerate([("orca pod", "orca whales travel"), ("pod racing", "fast"),
                                           ("whales", "blue whales and orca"), ("travel", "orca travel plans")]):
            self.note(store, a, f"n{n}", title, body)
        env = lookup(store, a, query="orca travel", explain=True)
        self.assertGreater(env["count"], 1)
        for item in env["results"]:
            legs = item["explain"]
            self.assertEqual(set(legs), {"lexical", "vector"})
            total = 0.0
            for leg in legs.values():
                if leg is not None:
                    self.assertEqual(leg["contribution"], 1 / (60 + leg["rank"]))
                    total += leg["contribution"]
            self.assertAlmostEqual(item["score"], total, places=15)
        self.assertEqual([r["rank"] for r in env["results"]], list(range(1, env["count"] + 1)))
        scores = [r["score"] for r in env["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertNotIn("explain", lookup(store, a, query="orca")["results"][0])


class QueryTokenTests(RetrievalTestCase):
    def test_terms_use_the_fts_tokenizer(self):
        store = self.open_store()
        self.assertEqual(lexical.query_terms(store.db, 'Héllo "wörld" hello-again NEAR(x)'),
                         ["hello", "world", "again", "near", "x"])
        self.assertEqual(lexical.match_expression(["a", "b"]), '"a" OR "b"')


if __name__ == "__main__":
    unittest.main()
