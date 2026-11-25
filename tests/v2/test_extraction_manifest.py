"""Disclosure manifest (spec 3.2): produced before any run, content-free, acknowledged by hash, stored."""
from __future__ import annotations

import json
import unittest

from .extraction_helpers import BASIC_LOG, ExtractionCase


class ManifestTests(ExtractionCase):
    def test_manifest_is_produced_before_any_run_and_names_everything(self):
        source = self.add_source(self.log(), categories=["user", "assistant"])
        planned = self.plan(source)
        manifest = planned["manifest"]
        for field in ("source_id", "adapter", "chunk_count", "chunk_hashes", "endpoint", "model_id", "local",
                      "estimated_tokens", "prompt_template_version", "redaction", "target", "budgets"):
            self.assertIn(field, manifest)
        self.assertEqual(manifest["endpoint"], "local:recording")
        self.assertEqual(manifest["model_id"], "recording-v1")
        self.assertTrue(manifest["local"])
        self.assertEqual(manifest["target"], {"scope": "private", "id": "alice"})
        self.assertEqual(manifest["chunk_count"], len(manifest["chunk_hashes"]))
        self.assertGreater(manifest["estimated_tokens"], 0)
        self.assertEqual(self.count("extraction_runs"), 0)   # planning starts nothing
        # Content-free: no record text anywhere in the manifest; chunk entries are 64-hex hashes.
        dumped = json.dumps(manifest)
        for _, text in BASIC_LOG:
            for word in ("staging", "team-blue", "thursdays"):
                self.assertNotIn(word, dumped)
        self.assertTrue(all(len(h) == 64 and int(h, 16) >= 0 for h in manifest["chunk_hashes"]))
        # Stored locally, before any run.
        row = self.store.db.execute("SELECT manifest_json, acknowledged_at FROM extraction_manifests"
                                    " WHERE manifest_hash = ?", (bytes.fromhex(planned["manifest_hash"]),)).fetchone()
        self.assertEqual(json.loads(row["manifest_json"]), manifest)
        self.assertIsNone(row["acknowledged_at"])

    def test_unacknowledged_run_is_refused_and_no_model_call_happens(self):
        source = self.add_source(self.log())
        model = self.model()
        # Never planned: any hash is refused.
        self.err(self.op.extract_run(source, "0" * 64, model=model), "disclosure_required")
        planned = self.plan(source)
        for ack in (None, "", "not-a-hash", "f" * 64, planned["manifest_hash"].upper()[:63]):
            self.err(self.op.extract_run(source, ack, model=model), "disclosure_required")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.count("extraction_runs"), 0)
        self.assertIsNone(self.store.db.execute("SELECT acknowledged_at FROM extraction_manifests").fetchone()[0])

        # The acknowledgement is stored with its time; then the run starts.
        acked = self.ok(self.op.extract_ack(source, planned["manifest_hash"]))
        self.assertTrue(acked["acknowledged_at"].endswith("Z"))
        self.assertIsNotNone(self.store.db.execute("SELECT acknowledged_at FROM extraction_manifests").fetchone()[0])
        run = self.ok(self.op.extract_run(source, planned["manifest_hash"], model=model))
        self.assertEqual(run["status"], "done")
        self.assertEqual(len(model.calls), 1)
        # The manifest lives with its run.
        stored = self.store.db.execute("SELECT manifest_json, manifest_hash FROM extraction_runs").fetchone()
        self.assertEqual(json.loads(stored["manifest_json"]), planned["manifest"])
        self.assertEqual(stored["manifest_hash"].hex(), planned["manifest_hash"])

    def test_hash_changes_with_configuration_and_content(self):
        path = self.log()
        source = self.add_source(path)
        first = self.plan(source)["manifest_hash"]
        self.assertEqual(self.plan(source)["manifest_hash"], first)   # deterministic
        self.ok(self.op.source_update(source, run_token_budget=40_000))
        second = self.plan(source)["manifest_hash"]
        self.assertNotEqual(second, first)
        self.ok(self.op.source_update(source, redaction_rules={"emails": False}))
        third = self.plan(source)["manifest_hash"]
        self.assertNotEqual(third, second)
        self.log(BASIC_LOG + [("user", "remember: one more line")])
        fourth = self.plan(source)["manifest_hash"]
        self.assertNotEqual(fourth, third)
        # An acknowledged but outdated manifest no longer starts a run.
        model = self.model()
        self.err(self.op.extract_run(source, third, model=model), "disclosure_required")
        self.assertEqual(model.calls, [])

    def test_disabled_source_never_runs(self):
        source = self.add_source(self.log(), enabled=False)
        planned = self.plan(source)   # planning is allowed while disabled
        model = self.model()
        self.err(self.op.extract_run(source, planned["manifest_hash"], model=model), "forbidden")
        self.assertEqual(model.calls, [])

    def test_model_client_must_match_the_manifest(self):
        source = self.add_source(self.log())
        planned = self.plan(source)
        for model in (self.model(model_id="other-model"), self.model(endpoint="local:other")):
            self.err(self.op.extract_run(source, planned["manifest_hash"], model=model), "disclosure_required")
            self.assertEqual(model.calls, [])


if __name__ == "__main__":
    unittest.main()
