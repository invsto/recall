"""Schema version 12 (extraction settings and manifests): a version-11 store upgrades in place."""
from __future__ import annotations

import sqlite3
import unittest

from recall_v2.store import Store, token_digest
from recall_v2.store.connection import create_file, raw_connect
from recall_v2.store.schema import APPLICATION_ID, MIGRATIONS, USER_VERSION

from .helpers import T0, StoreTestCase
from .test_schema_upgrade import schema_of


class V12UpgradeTests(StoreTestCase):
    def make_v11(self, path):
        create_file(path)
        db = raw_connect(path)
        db.executescript("BEGIN IMMEDIATE;\n" + MIGRATIONS[10] + "\n" + MIGRATIONS[11]
                         + f"\nPRAGMA application_id = {APPLICATION_ID};\nPRAGMA user_version = 11;"
                         "\nINSERT INTO meta (name, value) VALUES ('estimator', 'bytes3-v1');\nCOMMIT;")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("INSERT INTO principals (id, token_digest, created_at) VALUES ('alpha', ?, ?)",
                   (token_digest("tok-alpha"), T0))
        db.execute("INSERT INTO sources (id, adapter, uri, content_hash, captured_at, status)"
                   " VALUES ('01JD0000000000000000000009', 'jsonl-messages', '/x', ?, ?, 'live')", (bytes(32), T0))
        db.close()

    def test_version_11_store_upgrades_to_12(self):
        self.assertEqual(USER_VERSION, 12)
        directory = self.private_dir()
        old = directory / "old.sqlite3"
        self.make_v11(old)
        store = Store.open(old, clock=lambda: T0 + 1)
        self.addCleanup(store.close)
        db = store.db
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
        self.assertTrue({"source_settings", "extraction_manifests"} <= names)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)   # data kept
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        fresh = Store.open(directory / "fresh.sqlite3", create=True)
        self.addCleanup(fresh.close)
        self.assertEqual(schema_of(db), schema_of(fresh.db))

    def test_settings_checks(self):
        store = self.new_store()
        store.principal_add("alpha")
        db = store.db
        db.execute("INSERT INTO sources (id, adapter, uri, content_hash, captured_at, status)"
                   " VALUES ('S', 'jsonl-messages', '/x', ?, 1, 'live')", (bytes(32),))
        base = ("INSERT INTO source_settings (source_id, enabled, scope_kind, owner_id, project_id, categories_json,"
                " redaction_json, adapter_config_json, updated_at, run_token_budget, min_confidence)"
                " VALUES ('S', ?, ?, 'alpha', ?, '[]', '{}', '{}', 1, ?, ?)")
        for bad in ((2, "private", None, 10, 0.5), (0, "project", None, 10, 0.5), (0, "private", None, 0, 0.5),
                    (0, "private", None, 10, 1.5)):
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(base, bad)
        db.execute(base, (0, "private", None, 10, 0.5))
        self.assertEqual(db.execute("SELECT enabled, cloud_allowed, day_token_budget FROM source_settings")
                         .fetchone()[:], (0, 0, 200000))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO extraction_manifests (manifest_hash, source_id, manifest_json, planned_at)"
                       " VALUES (?, 'S', '{}', 1)", (b"short",))


if __name__ == "__main__":
    unittest.main()
