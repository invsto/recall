"""The generic JSONL message-log adapter (spec 8): read-only, configurable fields, stable chunks."""
from __future__ import annotations

from hashlib import sha256
import json
import os
import unittest

from recall_v2.adapters import ADAPTERS, JsonlMessageLog, SourceAdapter
from recall_v2.extraction.chunking import build_chunks
from recall_v2.extraction.redaction import Redactor

from .helpers import StoreTestCase


class JsonlAdapterTests(StoreTestCase):
    def setUp(self):
        self.dir = self.private_dir()
        self.path = self.dir / "messages.jsonl"
        lines = [
            {"who": "user", "body": "remember: alpha", "at": "2026-11-20T10:00:00Z", "conv": "c1"},
            "not json at all",
            {"who": "assistant", "body": "svc | owner = team", "at": 1700000000, "conv": "c1"},
            {"who": "user", "body": ""},
            {"who": "tool", "body": ["not", "text"]},
            {"body": "no role here"},
        ]
        self.path.write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n\n",
                             encoding="utf-8")
        self.adapter = JsonlMessageLog({"path": str(self.path),
                                        "fields": {"role": "who", "text": "body", "timestamp": "at", "session": "conv"}})

    def test_contract_and_configurable_fields(self):
        self.assertIsInstance(self.adapter, SourceAdapter)
        self.assertIs(ADAPTERS["jsonl-messages"], JsonlMessageLog)
        description = self.adapter.describe()
        self.assertEqual(description["adapter"], "jsonl-messages")
        self.assertIn("user", description["categories"])
        [ref] = self.adapter.discover()
        self.assertEqual(ref.content_hash, sha256(self.path.read_bytes()).digest())
        records = list(self.adapter.read(ref))
        self.assertEqual([(r.role, r.text) for r in records],
                         [("user", "remember: alpha"), ("assistant", "svc | owner = team"), (None, "no role here")])
        self.assertEqual(records[0].ts, "2026-11-20T10:00:00Z")
        self.assertEqual(records[1].ts, "1700000000")
        self.assertEqual(records[0].meta["session_id"], "c1")
        self.assertEqual(records[2].category, "unknown")
        self.assertEqual(len({r.hash for r in records}), 3)

    def test_read_only_and_stable_across_reads(self):
        before = (sha256(self.path.read_bytes()).hexdigest(), os.stat(self.path).st_mtime_ns,
                  os.stat(self.path).st_size, sorted(os.listdir(self.dir)))
        redactor = Redactor()
        plans = []
        for _ in range(3):
            refs = list(self.adapter.discover())
            plans.append(build_chunks(self.adapter, refs, ["user", "assistant", "unknown"], redactor))
        after = (sha256(self.path.read_bytes()).hexdigest(), os.stat(self.path).st_mtime_ns,
                 os.stat(self.path).st_size, sorted(os.listdir(self.dir)))
        self.assertEqual(before, after)
        self.assertEqual([[c.hash for c in p.chunks] for p in plans][0], [c.hash for c in plans[2].chunks])
        self.assertEqual([[c.hash for c in p.chunks] for p in plans].count([c.hash for c in plans[0].chunks]), 3)
        for chunk in plans[0].chunks:
            self.assertEqual(chunk.hash, sha256(chunk.text.encode("utf-8")).digest())

    def test_directory_discovery_and_chunk_limits(self):
        other = self.dir / "b.jsonl"
        other.write_text(json.dumps({"role": "user", "text": "é" * 7000}) + "\n", encoding="utf-8")
        (self.dir / "ignored.txt").write_text("x", encoding="utf-8")
        adapter = JsonlMessageLog({"path": str(self.dir)})
        refs = list(adapter.discover())
        self.assertEqual([os.path.basename(r.uri) for r in refs], ["b.jsonl", "messages.jsonl"])
        plan = build_chunks(adapter, refs, ["user"], Redactor())
        self.assertTrue(all(len(c.text.encode("utf-8")) <= 6000 for c in plan.chunks))
        self.assertEqual("".join(c.text for c in plan.chunks[:3]), "user: " + "é" * 7000 + "\n")

    def test_missing_path_discovers_nothing(self):
        self.assertEqual(list(JsonlMessageLog({"path": str(self.dir / "absent.jsonl")}).discover()), [])
        with self.assertRaises(ValueError):
            JsonlMessageLog({"path": "x", "fields": {"speaker": "who"}})


if __name__ == "__main__":
    unittest.main()
