"""Privacy (spec 3.2, D4): a non-local endpoint is refused unless the source is cloud-allowed.

No network client exists; the "cloud" model here is the recording double configured with a non-local
endpoint, so nothing is ever sent anywhere.
"""
from __future__ import annotations

import unittest

from recall_v2.extraction.sources import endpoint_is_local

from .extraction_helpers import ExtractionCase

CLOUD = "https://models.example.invalid/v1/extract"


class EndpointLocalityTests(unittest.TestCase):
    def test_locality(self):
        for endpoint in ("local:rules", "command:extractor", "http://127.0.0.1:8080/x", "http://localhost/x",
                         "https://[::1]:9000/x", "http://127.8.9.10/"):
            self.assertTrue(endpoint_is_local(endpoint), endpoint)
        for endpoint in (CLOUD, "http://10.0.0.5/x", "https://localhost.example.com/x", "http://192.168.1.2/"):
            self.assertFalse(endpoint_is_local(endpoint), endpoint)


class PrivacyTests(ExtractionCase):
    def test_private_source_with_cloud_endpoint_is_refused(self):
        source = self.add_source(self.log(), endpoint=CLOUD)
        self.err(self.op.extract_plan(source), "disclosure_required")
        model = self.model(endpoint=CLOUD, is_local=False)
        self.err(self.op.extract_run(source, "0" * 64, model=model), "disclosure_required")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.count("extraction_runs"), 0)

    def test_cloud_endpoint_allowed_only_when_explicitly_marked(self):
        source = self.add_source(self.log(), endpoint=CLOUD, cloud_allowed=True)
        planned = self.plan(source)
        self.assertFalse(planned["manifest"]["local"])
        self.assertTrue(planned["manifest"]["cloud_allowed"])
        model = self.model(endpoint=CLOUD, is_local=False)
        run = self.ok(self.op.extract_run(source, planned["manifest_hash"], model=model))
        self.assertEqual(run["status"], "done")
        self.assertEqual(len(model.calls), 1)
        # Revoking cloud_allowed refuses the next run again, before any call.
        self.ok(self.op.source_update(source, cloud_allowed=False))
        self.err(self.op.extract_run(source, planned["manifest_hash"], model=model), "disclosure_required")
        self.assertEqual(len(model.calls), 1)

    def test_private_source_with_local_endpoint_is_allowed(self):
        for endpoint in ("local:recording", "http://127.0.0.1:11434/api"):
            source = self.add_source(self.log(name=f"{len(endpoint)}.jsonl"), endpoint=endpoint)
            planned = self.plan(source)
            self.assertTrue(planned["manifest"]["local"])
            model = self.model(endpoint=endpoint)
            self.assertEqual(self.ok(self.op.extract_run(source, planned["manifest_hash"], model=model))["status"],
                             "done")

    def test_client_that_is_not_local_needs_cloud_allowed(self):
        source = self.add_source(self.log())
        planned = self.plan(source)
        model = self.model(is_local=False)
        self.err(self.op.extract_run(source, planned["manifest_hash"], model=model), "disclosure_required")
        self.assertEqual(model.calls, [])

    def test_project_source_follows_the_same_rule(self):
        target = {"scope": "project", "project": "proj", "principal": "alice"}
        source = self.add_source(self.log(), endpoint=CLOUD, target=target)
        self.err(self.op.extract_plan(source), "disclosure_required")


if __name__ == "__main__":
    unittest.main()
