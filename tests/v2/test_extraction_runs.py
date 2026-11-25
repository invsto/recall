"""Extraction output (spec 3.2): candidates only, with provenance; idempotent by chunk hash; cancellable;
never in default lookup or bundles; every step a content-free audit event."""
from __future__ import annotations

import unittest

from recall_v2.extraction.models import RuleBasedExtractor

from .extraction_helpers import BASIC_LOG, ExtractionCase
from .test_extraction_budget import big_log

HEDGED = ("user", "remember: maybe the cache is flaky")


class CandidatesOnlyTests(ExtractionCase):
    def test_private_target_gets_candidates_only_with_provenance(self):
        source = self.add_source(self.log(BASIC_LOG + [HEDGED]), categories=["user", "assistant"])
        planned, envelope = self.plan_and_run(source)
        run = self.ok(envelope)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["candidates_written"], 3)      # two notes, one fact; the system line is excluded
        self.assertEqual(run["candidates_discarded"], 1)    # the hedged note is below min_confidence 0.5
        rows = self.revisions_of_run(run["run_id"])
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["state"], "candidate")
            self.assertIsNone(row["accepted_at"])
            self.assertEqual(row["actor_id"], f"~extract:{run['run_id']}")
            self.assertEqual(row["source_id"], source)
            self.assertEqual(row["run_id"], run["run_id"])
            self.assertEqual(row["model_id"], "recording-v1")
            self.assertIn(row["chunk_hash"].hex(), planned["manifest"]["chunk_hashes"])
            self.assertTrue(0.5 <= row["confidence"] <= 1)
        self.assertEqual(self.count("item_revisions", "state = 'accepted'"), 0)
        self.assertEqual(self.count("items", "current_rev > 0"), 0)
        self.assertEqual(self.count("fts_items"), 0)

        # Default lookup and bundle never show them, even to the owner of the target scope.
        when = self.clock.now + 10 ** 9
        for client in (self.alice, self.bob):
            for query in ("staging", "thursdays", "region"):
                self.assertEqual(self.results(client.lookup(query=query, as_of=when, known_at=when)), [])
            self.assertEqual(self.results(client.lookup(subject="deploy-service", as_of=when, known_at=when)), [])
            bundle = client.bundle(as_of=when)
            self.assertTrue(bundle["ok"], bundle)
            self.assertEqual(bundle["items"], [])
            self.assertNotIn("staging", bundle["text"])

        # The operator reviews them through the governance path; sources show on each candidate.
        listed = self.results(self.op.review_list())
        self.assertEqual(len(listed), 3)
        self.assertTrue(all(c["sources"][0]["source_id"] == source for c in listed))
        fact = next(c for c in listed if c["kind"] == "fact")
        self.ok(self.op.review_approve(fact["item_id"], fact["rev"]))
        [hit] = self.results(self.alice.lookup(subject="deploy-service"))
        self.assertEqual(hit["fact"]["value"], "team-blue")

    def test_project_target_candidates_stay_out_of_project_bundles(self):
        target = {"scope": "project", "project": "proj", "principal": "alice"}
        source = self.add_source(self.log(), target=target)
        run = self.ok(self.plan_and_run(source)[1])
        self.assertGreater(run["candidates_written"], 0)
        rows = self.revisions_of_run(run["run_id"])
        self.assertTrue(all(r["state"] == "candidate" for r in rows))
        for client in (self.alice, self.bob):
            self.assertEqual(self.results(client.lookup(query="staging", project="proj")), [])
            self.assertEqual(client.bundle(project="proj")["items"], [])

    def test_invalid_output_fails_the_chunk_and_writes_nothing(self):
        source = self.add_source(self.log())
        model = self.model(respond=lambda text: [{"kind": "fact", "subject": "a", "confidence": 0.9}])
        run = self.ok(self.plan_and_run(source, model)[1])
        self.assertEqual((run["status"], run["chunks_failed"], run["candidates_written"]), ("done", 1, 0))
        self.assertEqual(self.count("item_revisions"), 0)
        # A failed chunk is retried on the next run.
        retry = self.ok(self.plan_and_run(source, self.model())[1])
        self.assertEqual((retry["chunks_done"], retry["chunks_skipped"]), (1, 0))


class IdempotencyTests(ExtractionCase):
    def test_rerunning_an_unchanged_source_extracts_nothing_new(self):
        source = self.add_source(self.log(big_log(3)))
        model = self.model()
        first = self.ok(self.plan_and_run(source, model)[1])
        self.assertEqual(first["chunks_done"], 3)
        revisions = self.count("item_revisions")
        audit_rows = self.count("audit")

        planned = self.plan(source)
        self.assertEqual(planned["manifest"]["pending_chunk_count"], 0)
        self.assertEqual(planned["manifest"]["estimated_tokens"], 0)
        second = self.ok(self.op.extract_run(source, planned["manifest_hash"], model=model))
        self.assertEqual((second["chunks_done"], second["chunks_skipped"], second["tokens_used"]), (0, 3, 0))
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(self.count("item_revisions"), revisions)

        # One appended record: only its chunk is new.
        self.log(big_log(3) + [("user", "remember: the new line " + "y" * 4000)])
        third = self.ok(self.plan_and_run(source, model)[1])
        self.assertEqual((third["chunks_done"], third["chunks_skipped"]), (1, 3))
        self.assertEqual(len(model.calls), 4)
        self.assertIn("the new line", model.calls[-1])
        self.assertEqual(self.count("item_revisions"), revisions + 1)
        self.assertGreater(self.count("audit"), audit_rows)

    def test_rule_based_extractor_is_deterministic(self):
        text = "user: remember: a thing\nassistant: svc | owner = team\n"
        extractor = RuleBasedExtractor()
        self.assertEqual(extractor.extract(text), extractor.extract(text))
        self.assertTrue(extractor.is_local)


class CancellationTests(ExtractionCase):
    def test_cancel_between_chunks_keeps_partial_candidates(self):
        source = self.add_source(self.log(big_log(5)))

        def cancel_on_second_call(n: int) -> None:
            if n == 2:
                [run_id] = [r[0] for r in self.store.db.execute(
                    "SELECT id FROM extraction_runs WHERE status = 'running'")]
                self.assertEqual(self.ok(self.op.extract_cancel(run_id))["status"], "cancelled")

        model = self.model(before_call=cancel_on_second_call)
        run = self.ok(self.plan_and_run(source, model)[1])
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(len(model.calls), 2)                   # stopped between chunks
        self.assertEqual((run["chunks_done"], run["chunks_not_run"]), (2, 3))
        rows = self.revisions_of_run(run["run_id"])
        self.assertEqual(len(rows), 2)                           # partial output kept ...
        self.assertTrue(all(r["state"] == "candidate" for r in rows))   # ... as candidates
        row = self.store.db.execute("SELECT status, ended_at FROM extraction_runs").fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNotNone(row["ended_at"])
        [listed] = self.results(self.op.extract_list(source))
        self.assertEqual((listed["status"], listed["chunks_done"], listed["candidates"]), ("cancelled", 2, 2))
        # The rest is picked up by the next run.
        rest = self.ok(self.plan_and_run(source)[1])
        self.assertEqual((rest["chunks_done"], rest["chunks_skipped"]), (3, 2))

    def test_cancel_of_unknown_run(self):
        self.err(self.op.extract_cancel("01JD0000000000000000000000"), "not_found")


class ExtractionAuditTests(ExtractionCase):
    def test_audit_events_are_content_free_and_chain_verifies(self):
        secret = "sk-live-4f9QzX2mN8vB7cR1tY6uP0wE"
        messages = big_log(2) + [("user", f"remember: key {secret} for staging " + "z" * 4000)]
        start = self.count("audit")
        source = self.add_source(self.log(messages))
        planned = self.plan(source)
        run = self.ok(self.op.extract_run(source, planned["manifest_hash"], model=self.model()))
        actor = f"~extract:{run['run_id']}"
        rows = [tuple(r) for r in self.store.db.execute(
            "SELECT actor, action FROM audit WHERE id > ? AND action <> 'save' ORDER BY id", (start,))]
        self.assertEqual(rows, [
            ("~operator", "source_add"), ("~operator", "source_update"), ("~operator", "extract_plan"),
            ("~operator", "extract_ack"), (actor, "extract_start"),
            (actor, "extract_chunk"), (actor, "extract_chunk"), (actor, "extract_chunk"),
            (actor, "extract_end"),
        ])
        saves = self.count("audit", "id > ? AND action = 'save' AND actor = ?", (start, actor))
        self.assertEqual(saves, run["candidates_written"])
        # Content-free: audit rows hold identifiers only.
        for row in self.store.db.execute("SELECT * FROM audit"):
            text = " ".join(str(v) for v in tuple(row))
            for word in ("staging", "remember", secret, "xxxx"):
                self.assertNotIn(word, text)
        self.assertTrue(self.ok(self.op.audit_verify())["ok"])


if __name__ == "__main__":
    unittest.main()
