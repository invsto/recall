"""Migration m3: a v2 service store (fixture from the pre-rename code) migrates to v3."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from recall_core.service.store import ServiceStore, StoreError, credential_digest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store_v2.sqlite3"
FIXTURE_TOKEN = "recall-fixture-token-alpha"  # credential digest stored in the fixture


def query(path, sql, params=()):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


class ServiceMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        os.chmod(self.directory, 0o700)
        self.db = self.directory / "service.sqlite3"
        shutil.copyfile(FIXTURE, self.db)
        os.chmod(self.db, 0o600)

    def test_v2_store_migrates_to_principals_with_intact_rows(self):
        self.assertEqual(query(self.db, "PRAGMA user_version"), [(2,)])
        self.assertEqual(ServiceStore(self.db).initialize(), {"schema_version": 3})
        self.assertEqual(query(self.db, "PRAGMA user_version"), [(3,)])
        self.assertEqual(
            query(self.db, "SELECT name FROM sqlite_schema WHERE type = 'table' AND name IN ('bots', 'principals')"),
            [("principals",)],
        )
        self.assertEqual(query(self.db, "SELECT id FROM principals ORDER BY id"), [("alpha",), ("beta",)])
        self.assertEqual(query(self.db, "SELECT bot, project FROM memberships"), [("alpha", "default")])
        self.assertEqual(query(self.db, "PRAGMA foreign_key_check"), [])

    def test_migration_keeps_a_verified_backup_of_the_v2_store(self):
        ServiceStore(self.db).initialize()
        backup = self.db.with_name(f"{self.db.name}.v2.bak")
        self.assertEqual(query(backup, "PRAGMA user_version"), [(2,)])
        self.assertEqual(query(backup, "SELECT id FROM bots ORDER BY id"), [("alpha",), ("beta",)])
        self.assertEqual(oct(os.stat(backup).st_mode & 0o777), "0o600")

    def test_migrated_store_still_accepts_the_existing_credential(self):
        store = ServiceStore(self.db)
        store.initialize()
        store.authorize(credential_digest(FIXTURE_TOKEN))  # raises if the credential was lost
        with self.assertRaises(StoreError) as refused:
            store.authorize(credential_digest("not-the-fixture-token"))
        self.assertEqual(refused.exception.result["error"], "unauthorized")

    def test_migrating_twice_changes_nothing(self):
        store = ServiceStore(self.db)
        store.initialize()
        before = query(self.db, "SELECT id, credential FROM principals ORDER BY id")
        self.assertEqual(store.initialize(), {"schema_version": 3})
        self.assertEqual(query(self.db, "SELECT id, credential FROM principals ORDER BY id"), before)
        self.assertEqual(store.health(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
