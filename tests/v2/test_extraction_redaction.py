"""Redaction runs before any model call: planted secrets never reach the (recording) model client."""
from __future__ import annotations

import unittest

from recall_v2.core.errors import RecallError
from recall_v2.extraction.redaction import Redactor, normalise_settings

from .extraction_helpers import ExtractionCase

# Secret-like strings (all synthetic). Each must never reach a model.
SECRETS = {
    "prefixed key": "sk-live-4f9QzX2mN8vB7cR1tY6uP0wE",
    "repo token": "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "cloud key id": "AKIAIOSFODNN7EXAMPLE",
    "chat token": "xoxb-123456789012-abcdefghijkl",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "password assignment": "hunter2-Correct-Horse",
    "url credential": "s3cr3tP4ss",
    "bearer": "abcdef0123456789abcdef0123456789",
    "email": "carol.owner@example.org",
    "private block": "my diary says the vault code is 9921",
    "pem": "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7",
}

LOG = [
    ("user", f"remember: the deploy key is {SECRETS['prefixed key']}"),
    ("user", f"token for the repo: {SECRETS['repo token']} and {SECRETS['cloud key id']}"),
    ("assistant", f"chat hook uses {SECRETS['chat token']}; session {SECRETS['jwt']}"),
    ("user", f"password = {SECRETS['password assignment']}"),
    ("user", f"connect with postgres://admin:{SECRETS['url credential']}@db.internal:5432/app"),
    ("user", f"Authorization: Bearer {SECRETS['bearer']}"),
    ("user", f"remember: mail {SECRETS['email']} about the release"),
    ("user", f"<private>{SECRETS['private block']}</private> remember: tidy the backlog"),
    ("user", f"-----BEGIN RSA PRIVATE KEY-----\n{SECRETS['pem']}\n-----END RSA PRIVATE KEY-----"),
    ("assistant", "service-x | region = eu-west-2"),
]


class RedactorUnitTests(unittest.TestCase):
    def test_each_rule_replaces_its_matches(self):
        redactor = Redactor()
        text = "\n".join(t for _, t in LOG)
        clean, counts = redactor.redact(text)
        for name, secret in SECRETS.items():
            self.assertNotIn(secret, clean, name)
        self.assertIn("[REDACTED:secrets]", clean)
        self.assertIn("[REDACTED:emails]", clean)
        self.assertIn("[REDACTED:private_tags]", clean)
        self.assertEqual(counts["emails"], 1)
        self.assertEqual(counts["private_tags"], 1)
        self.assertGreaterEqual(counts["secrets"], 9)
        self.assertIn("service-x | region = eu-west-2", clean)   # ordinary text survives
        self.assertEqual(redactor.redact(text), (clean, counts))   # deterministic

    def test_rules_can_be_switched_and_extended(self):
        clean, counts = Redactor({"emails": False}).redact("write to a@b.example")
        self.assertEqual(clean, "write to a@b.example")
        self.assertNotIn("emails", counts)
        clean, counts = Redactor({"custom": [{"name": "codename", "pattern": r"\bProject Falcon\b"}]}).redact(
            "Project Falcon ships")
        self.assertEqual(clean, "[REDACTED:codename] ships")
        self.assertEqual(counts["codename"], 1)
        for bad in ({"nope": True}, {"secrets": "yes"}, {"custom": [{"name": "x", "pattern": "("}]},
                    {"custom": [{"name": "secrets", "pattern": "a"}]}, {"custom": [{"name": "e", "pattern": "a*"}]}):
            with self.assertRaises(RecallError):
                normalise_settings(bad)

    def test_unclosed_private_tag_hides_the_rest(self):
        clean, _ = Redactor().redact("keep this <private>hide this and everything after")
        self.assertEqual(clean, "keep this [REDACTED:private_tags]")


class RedactionBeforeModelTests(ExtractionCase):
    def test_no_planted_secret_reaches_the_model_client(self):
        source = self.add_source(self.log(LOG))
        planned = self.plan(source)
        counts = planned["manifest"]["redaction"]["counts"]
        self.assertEqual(counts["emails"], 1)
        self.assertEqual(counts["private_tags"], 1)
        model = self.model()
        run = self.ok(self.op.extract_run(source, planned["manifest_hash"], model=model))
        self.assertEqual(run["status"], "done")
        self.assertTrue(model.calls)
        received = "".join(model.calls)
        for name, secret in SECRETS.items():
            self.assertNotIn(secret, received, name)
        # The client got the redacted text: markers present, ordinary content intact.
        self.assertIn("[REDACTED:secrets]", received)
        self.assertIn("[REDACTED:emails]", received)
        self.assertIn("[REDACTED:private_tags]", received)
        self.assertIn("service-x | region = eu-west-2", received)
        # Nothing secret was stored either (candidates come from redacted text only).
        dump = "\n".join(str(tuple(r)) for r in self.store.db.execute("SELECT * FROM item_revisions"))
        dump += "\n".join(str(tuple(r)) for r in self.store.db.execute("SELECT * FROM fact_versions"))
        for name, secret in SECRETS.items():
            self.assertNotIn(secret, dump, name)


if __name__ == "__main__":
    unittest.main()
