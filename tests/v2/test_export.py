"""Exports (spec 6): JSONL per project or principal with provenance, validity and state; deleted content
absent; operator only."""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import stat
import unittest

from recall_v2.api import Client
from recall_v2.core.errors import RecallError
from recall_v2.lifecycle import export as export_module

from .extraction_helpers import ExtractionCase

CANARY = "zqxexportcanary2207"


class ExportTests(ExtractionCase):
    def read(self, path):
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]

    def test_project_export_has_provenance_validity_state_and_no_tombstones(self):
        accepted = self.project_note(self.alice, "plan", "release plan", valid_to=self.clock.now + 10 ** 12)
        self.ok(self.op.review_approve(accepted["id"], 1))
        candidate = self.project_note(self.bob, "idea", "bob idea")
        gone = self.project_note(self.alice, "secret", f"remove {CANARY}")
        self.ok(self.op.review_approve(gone["id"], 1))
        self.ok(self.op.delete(gone["id"]))
        source = self.add_source(self.log([("assistant", "svc | owner = team-blue")]),
                                 target={"scope": "project", "project": "proj", "principal": "alice"})
        _, run = self.plan_and_run(source)
        self.ok(run)
        private = self.ok(self.alice.save(kind="note", request_id="p", key="mine", body="alice private"))

        out = self.ok(self.op.export(project="proj"))
        path = Path(out["path"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        raw = path.read_bytes()
        self.assertNotIn(CANARY.encode(), raw)
        lines = self.read(path)
        self.assertEqual(out["lines"], len(lines))
        ids = {line["id"] for line in lines}
        self.assertIn(accepted["id"], ids)
        self.assertIn(candidate["id"], ids)
        self.assertNotIn(gone["id"], ids)          # tombstones are not exported
        self.assertNotIn(private["id"], ids)       # other scopes are not exported
        plan = next(line for line in lines if line["id"] == accepted["id"])
        self.assertEqual((plan["state"], plan["body"], plan["scope"]), ("accepted", "release plan",
                                                                         {"type": "project", "id": "proj"}))
        self.assertIsNotNone(plan["valid_from"])
        self.assertIsNotNone(plan["valid_to"])
        self.assertEqual(plan["reviewed_by"], "~operator")
        self.assertEqual(next(line for line in lines if line["id"] == candidate["id"])["state"], "candidate")
        extracted = [line for line in lines if line["provenance"]]
        self.assertEqual(len(extracted), 1)
        prov = extracted[0]["provenance"][0]
        self.assertEqual((prov["source_id"], prov["source_status"]), (source, "live"))
        self.assertEqual(len(prov["chunk_hash"]), 64)
        self.assertIsNotNone(prov["run_id"])
        self.assertEqual(extracted[0]["fact"]["value"], "team-blue")
        artifact = self.store.db.execute("SELECT kind FROM artifacts WHERE path = ?", (str(path),)).fetchone()
        self.assertEqual(artifact[0], "export")
        last = self.store.db.execute("SELECT action, project_id FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(last), ("export", "proj"))

    def test_principal_export(self):
        mine = self.ok(self.alice.save(kind="note", request_id="a", key="mine", body="alice private"))
        self.ok(self.bob.save(kind="note", request_id="b", key="his", body="bob private"))
        out = self.ok(self.op.export(principal="alice"))
        lines = self.read(out["path"])
        self.assertEqual([line["id"] for line in lines], [mine["id"]])
        self.assertEqual(lines[0]["scope"], {"type": "private", "id": "alice"})

    def test_arguments(self):
        self.err(self.op.export(), "invalid_input")
        self.err(self.op.export(project="proj", principal="alice"), "invalid_input")
        self.err(self.op.export(project="nope"), "not_found")
        target = self.private_dir() / "e.jsonl"
        target.write_text("x")
        self.err(self.op.export(project="proj", path=str(target)), "backup_exists")
        self.assertEqual(target.read_text(), "x")

    def test_principal_calls_are_refused(self):
        self.assertFalse(hasattr(Client, "export"))
        self.assertFalse(hasattr(self.alice, "export"))
        principal = self.store.authenticate(self.tokens["alice"])
        for target in (self.alice, principal, self.store, None, "~operator"):
            with self.assertRaises(RecallError) as raised:
                export_module.export(target, project="proj")
            self.assertEqual(raised.exception.code, "forbidden")
        self.assertEqual(self.count("artifacts", "kind = 'export'"), 0)
        self.assertNotIn("export", {n for n, _ in inspect.getmembers(Client, inspect.isfunction)})


if __name__ == "__main__":
    unittest.main()
