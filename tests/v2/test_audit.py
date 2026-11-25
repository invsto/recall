"""Audit: append-only, content-free, hash-chained; verify passes and any tampering fails (spec 5)."""
from __future__ import annotations

from hashlib import sha256
import unittest

from recall_v2.core.normalise import canonical_json
from recall_v2.store import audit

from .helpers import StoreTestCase

CANARY = "CANARY-7f3a-secret-body"


class AuditTests(StoreTestCase):
    def setUp(self):
        self.store = self.new_store()
        alice = self.add_principal(self.store, "alice")
        self.store.project_add("proj")
        self.store.member_add("alice", "proj")
        self.store.save(alice, kind="note", request_id="a", key="n1", title=CANARY, body=CANARY)
        out = self.store.save(alice, kind="note", request_id="b", key="n2", body=CANARY, scope="project",
                              project="proj")
        self.store.review_approve(out["item_id"], out["rev"])
        fact = self.store.save(alice, kind="fact", request_id="c",
                               fact={"subject": CANARY, "predicate": "p", "value": CANARY})
        self.store.retract(alice, item_id=fact["item_id"], rev=1, request_id="d")
        self.db = self.store.db
        self.rows = self.db.execute("SELECT * FROM audit ORDER BY id").fetchall()

    def test_verify_passes(self):
        result = self.store.verify_audit()
        self.assertEqual(result, {"ok": True, "checked": len(self.rows), "first_bad_id": None})
        self.assertGreaterEqual(len(self.rows), 8)
        self.assertEqual(self.rows[0]["prev_hash"], bytes(32))
        first = dict(self.rows[0])
        payload = canonical_json({k: first[k] for k in ("id", "at", "actor", "action", "item_id", "rev",
                                                         "project_id", "legacy")})
        self.assertEqual(first["hash"], sha256(bytes(32) + payload.encode()).digest())

    def test_events_are_content_free(self):
        for row in self.rows:
            for value in tuple(row):
                if isinstance(value, str):
                    self.assertNotIn(CANARY, value)
        self.assertEqual({row["action"] for row in self.rows} - audit.ACTIONS, set())
        with self.assertRaises(ValueError):
            audit.append(self.db, at=1, actor="x", action="saved body text")

    def test_updates_are_blocked(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE audit SET actor = 'mallory' WHERE id = 1")

    def tamper_cases(self):
        columns = {"at": 1, "actor": "mallory", "action": "delete", "item_id": "X", "rev": 99, "project_id": "other",
                   "legacy": 1, "prev_hash": b"\x00" * 31 + b"\x01", "hash": b"\x02" * 32}
        for row in self.rows:
            for column, value in columns.items():
                yield row["id"], column, value

    def test_tampering_any_row_or_column_fails_verify(self):
        self.db.execute("DROP TRIGGER audit_append_only")   # an attacker with file access can do this
        for row_id, column, value in self.tamper_cases():
            with self.subTest(row=row_id, column=column):
                self.db.execute("SAVEPOINT tamper")
                self.db.execute(f"UPDATE audit SET {column} = ? WHERE id = ?", (value, row_id))
                result = audit.verify_chain(self.db)
                self.db.execute("ROLLBACK TO tamper")
                self.db.execute("RELEASE tamper")
                self.assertFalse(result["ok"])
                self.assertEqual(result["first_bad_id"], row_id)
        self.assertTrue(audit.verify_chain(self.db)["ok"])

    def test_deleting_or_reordering_a_middle_row_fails_verify(self):
        middle = self.rows[len(self.rows) // 2]["id"]
        self.db.execute("SAVEPOINT t")
        self.db.execute("DELETE FROM audit WHERE id = ?", (middle,))
        self.assertFalse(audit.verify_chain(self.db)["ok"])
        self.db.execute("ROLLBACK TO t")
        self.db.execute("RELEASE t")
        self.assertTrue(audit.verify_chain(self.db)["ok"])

    def test_anchor_after_pruning(self):
        cut = self.rows[2]
        self.db.execute("DELETE FROM audit WHERE id <= ?", (cut["id"],))
        self.assertFalse(audit.verify_chain(self.db)["ok"])
        self.db.execute("INSERT INTO meta (name, value) VALUES ('audit_anchor', ?)", (cut["hash"].hex(),))
        self.assertTrue(audit.verify_chain(self.db)["ok"])


if __name__ == "__main__":
    unittest.main()
