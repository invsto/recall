"""Schema upgrades: a version-10 store upgrades in place to the current version; a fresh store has the same schema."""
from __future__ import annotations

import unittest

from recall_v2.retrieval import lookup
from recall_v2.store import Store, token_digest
from recall_v2.store.connection import create_file, raw_connect
from recall_v2.store.schema import APPLICATION_ID, MIGRATIONS, USER_VERSION

from .helpers import T0, StoreTestCase


def schema_of(db):
    return sorted(tuple(row) for row in db.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema"))


class UpgradeTests(StoreTestCase):
    def make_v10(self, path):
        create_file(path)
        db = raw_connect(path)
        db.executescript("BEGIN IMMEDIATE;\n" + MIGRATIONS[10] + f"\nPRAGMA application_id = {APPLICATION_ID};"
                         "\nPRAGMA user_version = 10;\nINSERT INTO meta (name, value) VALUES ('estimator', 'bytes3-v1');"
                         "\nCOMMIT;")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("INSERT INTO principals (id, token_digest, created_at) VALUES ('alpha', ?, ?)",
                   (token_digest("tok-alpha"), T0))
        db.execute("INSERT INTO items (id, kind, scope_kind, owner_id, key, current_rev, created_at)"
                   " VALUES ('01JD0000000000000000000001', 'note', 'private', 'alpha', 'k', 1, ?)", (T0,))
        db.execute("INSERT INTO item_revisions (item_id, rev, state, title, body, valid_from, actor_id, recorded_at,"
                   " accepted_at) VALUES ('01JD0000000000000000000001', 1, 'accepted', 'Legacy note',"
                   " 'narwhal sighting', ?, 'alpha', ?, ?)", (T0, T0, T0))
        db.execute("INSERT INTO fts_items (rowid, title, body) SELECT rowid, title, body FROM item_revisions")
        db.close()

    def test_version_10_store_upgrades(self):
        self.assertEqual(USER_VERSION, 12)
        directory = self.private_dir()
        old = directory / "old.sqlite3"
        self.make_v10(old)
        store = Store.open(old, clock=lambda: T0 + 1)
        self.addCleanup(store.close)
        db = store.db
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], USER_VERSION)
        self.assertEqual(db.execute("SELECT v FROM fts_items_config WHERE k = 'secure-delete'").fetchone()[0], 1)
        principal = store.authenticate("tok-alpha")
        [hit] = lookup(store, principal, query="narwhal")["results"]
        self.assertEqual(hit["title"], "Legacy note")
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        fresh = Store.open(directory / "fresh.sqlite3", create=True)
        self.addCleanup(fresh.close)
        self.assertEqual(schema_of(db), schema_of(fresh.db))
        self.assertEqual(fresh.db.execute("PRAGMA user_version").fetchone()[0], USER_VERSION)

    def test_failed_upgrade_rolls_back(self):
        directory = self.private_dir()
        old = directory / "old.sqlite3"
        self.make_v10(old)
        db = raw_connect(old)
        db.execute("CREATE TABLE fts_items_vocab (x)")  # collides with the version-11 step
        db.close()
        self.assertCode("migration_failed", Store.open, old)
        db = raw_connect(old)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)
        self.assertIsNone(db.execute("SELECT 1 FROM sqlite_schema WHERE name = 'items_by_kind_key'").fetchone())
        db.close()


if __name__ == "__main__":
    unittest.main()
