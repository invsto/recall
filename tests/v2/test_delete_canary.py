"""Deletion (spec 6): tombstones, content nulled, index rows removed, the erasure ledger, the principal
forget path, source deletion (3.7), and the deletion canary: after maintenance no canary byte remains in
the live store file or in the FTS shadow tables."""
from __future__ import annotations

import json
import unittest

from recall_v2.lifecycle import deletion

from .extraction_helpers import ExtractionCase
from .governance_helpers import GovernanceCase

CANARY = "zqxcanary9f2b"           # lowercase: the FTS index stores folded terms
CANARY_UPPER = "ZQXCANARY9F2B"


class CanaryMixin:
    def assert_canary_absent(self, canary: str = CANARY):
        db = self.store.db
        needles = {canary.encode(), canary.upper().encode()}
        # FTS: no match, no vocabulary term, nothing in any shadow table.
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fts_items WHERE fts_items MATCH ?", (f'"{canary}"',)).fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fts_items_vocab WHERE term = ?", (canary,)).fetchone()[0], 0)
        shadows = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type = 'table'"
                                            " AND name LIKE 'fts_items_%' AND name <> 'fts_items_vocab'")]
        self.assertTrue({"fts_items_data", "fts_items_content", "fts_items_docsize", "fts_items_config"} <= set(shadows))
        for table in shadows:
            for row in db.execute(f"SELECT * FROM {table}"):
                for value in tuple(row):
                    raw = value if isinstance(value, bytes) else str(value).encode()
                    for needle in needles:
                        self.assertNotIn(needle, raw, f"{table} holds the canary")
        # The raw bytes of the live file (and of the WAL, if any).
        data = self.path.read_bytes()
        for needle in needles:
            self.assertNotIn(needle, data, "the live store file holds the canary")
        wal = self.path.with_name(self.path.name + "-wal")
        if wal.exists():
            for needle in needles:
                self.assertNotIn(needle, wal.read_bytes())


class DeleteTests(CanaryMixin, GovernanceCase):
    def plant(self):
        note = self.ok(self.alice.save(kind="note", request_id="c1", key=f"key-{CANARY}", title=f"title {CANARY}",
                                       body=f"secret body {CANARY} " * 40))   # overflow pages too
        fact = self.private_fact(self.alice, f"value-{CANARY}", subject=f"subj-{CANARY}", predicate="code")
        fact2 = self.private_fact(self.alice, f"second-{CANARY_UPPER}", subject=f"subj-{CANARY}", predicate="code")
        self.assertEqual(fact2["id"], fact["id"])
        self.assertEqual(fact2["rev"], 2)
        return note, fact

    def test_canary_absent_from_raw_file_and_fts_after_maintenance(self):
        note, fact = self.plant()
        self.assertEqual(len(self.results(self.alice.lookup(query=CANARY))), 2)
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertIn(CANARY.encode(), self.path.read_bytes())       # the canary really reached the file

        deleted = self.op.delete(note["id"])
        self.assertTrue(deleted["ok"], deleted)
        forgotten = self.alice.forget(item_id=fact["id"], request_id="f1")
        self.assertTrue(forgotten["ok"], forgotten)
        self.assertEqual(self.results(self.alice.lookup(query=CANARY)), [])

        self.ok(self.op.maintain())
        self.assert_canary_absent()

    def test_tombstone_and_nulled_content(self):
        note, fact = self.plant()
        self.ok(self.alice.pin(item_id=note["id"]))
        envelope = self.op.delete(note["id"])
        tomb = self.ok(envelope)
        self.assertEqual((tomb["id"], tomb["kind"], tomb["rev"], tomb["scope"]),
                         (note["id"], "note", 1, {"type": "private", "id": "alice"}))
        self.assertIsNone(tomb["title"])
        self.assertIsNone(tomb["body"])
        self.assertIsNotNone(tomb["deleted_at"])
        self.assertEqual(envelope["not_guaranteed"], list(deletion.NOT_GUARANTEED))
        self.assertEqual(envelope["artifacts"], [])
        item = self.store.db.execute("SELECT * FROM items WHERE id = ?", (note["id"],)).fetchone()
        self.assertIsNone(item["key"])
        self.assertIsNotNone(item["deleted_at"])
        self.assertEqual(item["kind"], "note")
        rev = self.revision(note["id"], 1)
        self.assertEqual((rev["title"], rev["body"], rev["body_hash"], rev["meta_json"]), (None, None, None, None))
        for table in ("pins", "receipts", "embeddings"):
            self.assertEqual(self.count(table, "item_id = ?", (note["id"],)), 0, table)
        self.assertEqual(self.count("fts_items", "rowid IN (SELECT rowid FROM item_revisions WHERE item_id = ?)",
                                    (note["id"],)), 0)
        self.assertEqual(self.count("erasures", "item_id = ?", (note["id"],)), 1)
        self.err(self.alice.get(item_id=note["id"]), "not_found")
        self.err(self.alice.history(item_id=note["id"]), "not_found")
        ledger = [json.loads(line) for line in self.store.ledger_path.read_text().splitlines()]
        self.assertEqual([(e["item_id"], e["rev"]) for e in ledger], [(note["id"], 1)])
        self.assertEqual(self.store.ledger_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.verify_audit()["ok"], True)
        # deleting again is not_found; a later save of the same key creates a new item
        self.err(self.op.delete(note["id"]), "not_found")
        again = self.ok(self.alice.save(kind="note", request_id="c9", key=f"key-{CANARY}", body="new"))
        self.assertNotEqual(again["id"], note["id"])

    def test_fact_delete_removes_every_version_body(self):
        _, fact = self.plant()
        self.ok(self.alice.retract(item_id=fact["id"], rev=2, request_id="r1"))
        self.ok(self.op.delete(fact["id"]))
        rows = self.store.db.execute("SELECT * FROM fact_versions WHERE item_id = ? ORDER BY rev", (fact["id"],)).fetchall()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual((row["subject"], row["predicate"], row["value_json"]), (None, None, None))
            self.assertIn(row["op"], ("assert", "retract"))
        self.assertEqual(self.count("item_revisions", "item_id = ? AND body_hash IS NULL AND meta_json IS NULL",
                                    (fact["id"],)), 3)
        self.assertEqual(self.results(self.alice.lookup(subject=f"subj-{CANARY}")), [])
        self.assertIsNone(self.store.db.execute("SELECT key FROM items WHERE id = ?", (fact["id"],)).fetchone()[0])

    def test_forget_permissions(self):
        own = self.ok(self.alice.save(kind="note", request_id="o1", key="mine", body="b"))
        cand = self.project_note(self.bob, "draft", "bob draft")
        accepted = self.project_note(self.alice, "shared", "shared text")
        self.ok(self.op.review_approve(accepted["id"], 1))
        self.err(self.bob.forget(item_id=own["id"], request_id="x1"), "not_found")       # invisible
        self.err(self.alice.forget(item_id=cand["id"], request_id="x2"), "not_found")    # bob's candidate
        self.err(self.bob.forget(item_id=accepted["id"], request_id="x3"), "forbidden")  # accepted project item
        self.err(self.alice.forget(item_id=accepted["id"], request_id="x4"), "forbidden")
        envelope = self.bob.forget(item_id=cand["id"], request_id="x5")
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(envelope["results"][0]["body"], None)
        self.assertIn("not_guaranteed", envelope)
        replay = self.bob.forget(item_id=cand["id"], request_id="x5")
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(replay["results"][0]["id"], cand["id"])
        self.ok(self.alice.forget(item_id=own["id"], request_id="x6"))
        self.assertEqual(self.count("items", "deleted_at IS NOT NULL"), 2)

    def test_delete_lists_predating_artifacts(self):
        note, _ = self.plant()
        made = self.ok(self.op.backup())
        envelope = self.op.delete(note["id"])
        self.assertEqual([a["path"] for a in envelope["artifacts"]], [made["path"]])
        self.assertEqual(self.alice.forget(item_id=self.private_fact(self.alice, "z", subject="q")["id"],
                                           request_id="q1")["artifacts_predating"], 1)


class SourceDeleteTests(CanaryMixin, ExtractionCase):
    def test_source_delete_rejects_and_erases_candidates(self):
        log = self.log([("user", f"remember: the vault code is {CANARY}"),
                        ("assistant", "deploy-service | owner = team-blue")])
        source = self.add_source(log)
        _, envelope = self.plan_and_run(source)
        run = self.ok(envelope)
        self.assertEqual(run["candidates_written"], 2)
        rows = self.revisions_of_run(run["run_id"])
        fact_item = next(r["item_id"] for r in rows if r["body"] is None)
        note_item = next(r["item_id"] for r in rows if r["body"] is not None)
        self.ok(self.op.review_approve(fact_item, 1))     # the fact is accepted, the note stays a candidate

        out = self.ok(self.op.source_delete(source))
        self.assertEqual(out["rejected"], [{"item_id": note_item, "rev": 1}])
        self.assertEqual(out["erased_items"], [note_item])
        self.assertEqual(out["cascaded"], [])
        src = self.store.db.execute("SELECT * FROM sources WHERE id = ?", (source,)).fetchone()
        self.assertEqual((src["uri"], src["status"]), (None, "deleted"))
        self.assertEqual(len(src["content_hash"]), 32)
        rev = self.revision(note_item, 1)
        self.assertEqual((rev["state"], rev["review_note"], rev["body"]), ("rejected", "source_deleted", None))
        # the accepted fact stays, showing the deleted source
        got = self.ok(self.alice.get(item_id=fact_item))
        self.assertEqual([s["deleted"] for s in got["sources"]], [True])
        self.assertEqual(got["sources"][0]["uri"], None)
        self.ok(self.op.maintain())
        self.assert_canary_absent()

    def test_cascade_deletes_accepted_items(self):
        source = self.add_source(self.log([("assistant", "deploy-service | owner = team-blue")]))
        _, envelope = self.plan_and_run(source)
        run = self.ok(envelope)
        item = self.revisions_of_run(run["run_id"])[0]["item_id"]
        self.ok(self.op.review_approve(item, 1))
        out = self.ok(self.op.source_delete(source, cascade=True))
        self.assertEqual(out["cascaded"], [item])
        self.assertIn("not_guaranteed", out)
        self.assertEqual(self.count("items", "id = ? AND deleted_at IS NOT NULL", (item,)), 1)

    def test_candidate_with_another_live_source_stays(self):
        log = self.log([("user", "remember: shared fact about otters")])
        first = self.add_source(log)
        _, envelope = self.plan_and_run(first)
        run = self.ok(envelope)
        item = self.revisions_of_run(run["run_id"])[0]["item_id"]
        second = self.add_source(self.log([("user", "x")], name="b.jsonl"))
        self.store.db.execute("INSERT INTO provenance (item_id, rev, source_id) VALUES (?, 1, ?)", (item, second))
        out = self.ok(self.op.source_delete(first))
        self.assertEqual(out["rejected"], [])
        self.assertEqual(self.revision(item, 1)["state"], "candidate")

    def test_source_delete_is_operator_only(self):
        from recall_v2.core.errors import RecallError
        with self.assertRaises(RecallError) as raised:
            deletion.source_delete(self.alice, "S")
        self.assertEqual(raised.exception.code, "forbidden")
        with self.assertRaises(RecallError) as raised:
            deletion.delete(self.store, "I")
        self.assertEqual(raised.exception.code, "forbidden")


if __name__ == "__main__":
    unittest.main()
