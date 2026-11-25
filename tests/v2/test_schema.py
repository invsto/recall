"""Schema: every CHECK, UNIQUE, NOT NULL and FOREIGN KEY rejects a violating row; pragmas; version gates."""
from __future__ import annotations

from hashlib import sha256
import sqlite3
import unittest
from unittest import mock

from recall_v2.store import Store, connection
from recall_v2.store.schema import APPLICATION_ID, USER_VERSION

from .helpers import StoreTestCase

D32 = b"\x01" * 32


class SchemaConstraintTests(StoreTestCase):
    def setUp(self):
        self.store = self.new_store()
        self.db = self.store.db
        x = self.db.execute
        x("INSERT INTO principals (id, token_digest, created_at) VALUES ('p1', ?, 1)", (D32,))
        x("INSERT INTO principals (id, token_digest, created_at) VALUES ('p2', NULL, 1)")
        x("INSERT INTO projects (id, created_at) VALUES ('proj', 1)")
        x("INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at) VALUES ('I1', 'note', 'private', 'p1', 'k', 1)")
        x("INSERT INTO items (id, kind, scope_kind, owner_id, project_id, key, created_at)"
          " VALUES ('I2', 'brief', 'project', 'p1', 'proj', 'b', 1)")
        x("INSERT INTO item_revisions (item_id, rev, state, body, valid_from, actor_id, recorded_at, accepted_at)"
          " VALUES ('I1', 1, 'accepted', 'x', 1, 'p1', 1, 1)")
        x("INSERT INTO item_revisions (item_id, rev, state, valid_from, actor_id, recorded_at)"
          " VALUES ('I2', 1, 'candidate', 1, 'p1', 1)")
        x("INSERT INTO sources (id, adapter, content_hash, captured_at, status) VALUES ('S1', 'md', x'00', 1, 'live')")
        x("INSERT INTO extraction_runs (id, source_id, model_id, endpoint, manifest_json, manifest_hash, started_at, status)"
          " VALUES ('R1', 'S1', 'm', 'local', '{}', x'00', 1, 'running')")

    REJECTED = [
        # principals
        ("token digest length", "INSERT INTO principals (id, token_digest, created_at) VALUES ('p3', x'0102', 1)"),
        ("token digest unique", "INSERT INTO principals (id, token_digest, created_at) VALUES ('p3', ?, 1)", (D32,)),
        ("principal created_at NOT NULL", "INSERT INTO principals (id) VALUES ('p3')"),
        ("principal id NOT NULL", "INSERT INTO principals (id, created_at) VALUES (NULL, 1)"),
        ("principal id unique", "INSERT INTO principals (id, created_at) VALUES ('p1', 1)"),
        ("STRICT type", "INSERT INTO principals (id, created_at) VALUES ('p3', 'yesterday')"),
        # projects / memberships
        ("project created_at NOT NULL", "INSERT INTO projects (id) VALUES ('q')"),
        ("membership principal FK", "INSERT INTO memberships VALUES ('nobody', 'proj', 1)"),
        ("membership project FK", "INSERT INTO memberships VALUES ('p1', 'nowhere', 1)"),
        ("membership added_at NOT NULL", "INSERT INTO memberships (principal_id, project_id) VALUES ('p1', 'proj')"),
        # items
        ("item kind", "INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at) VALUES ('X', 'memo', 'private', 'p1', 'x', 1)"),
        ("item scope", "INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at) VALUES ('X', 'note', 'team', 'p1', 'x', 1)"),
        ("project scope needs project_id", "INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at) VALUES ('X', 'note', 'project', 'p1', 'x', 1)"),
        ("private scope has no project_id", "INSERT INTO items (id, kind, scope_kind, owner_id, project_id, key, created_at) VALUES ('X', 'note', 'private', 'p1', 'proj', 'x', 1)"),
        ("checkpoint is private", "INSERT INTO items (id, kind, scope_kind, owner_id, project_id, key, created_at) VALUES ('X', 'checkpoint', 'project', 'p1', 'proj', 'x', 1)"),
        ("brief is project", "INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at) VALUES ('X', 'brief', 'private', 'p1', 'x', 1)"),
        ("live item needs key", "INSERT INTO items (id, kind, scope_kind, owner_id, created_at) VALUES ('X', 'note', 'private', 'p1', 1)"),
        ("deleted item has no key", "INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at, deleted_at) VALUES ('X', 'note', 'private', 'p1', 'x', 1, 2)"),
        ("private key unique", "INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at) VALUES ('X', 'note', 'private', 'p1', 'k', 1)"),
        ("project key unique", "INSERT INTO items (id, kind, scope_kind, owner_id, project_id, key, created_at) VALUES ('X', 'brief', 'project', 'p2', 'proj', 'b', 1)"),
        ("item owner FK", "INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at) VALUES ('X', 'note', 'private', 'nobody', 'x', 1)"),
        ("item project FK", "INSERT INTO items (id, kind, scope_kind, owner_id, project_id, key, created_at) VALUES ('X', 'note', 'project', 'p1', 'nowhere', 'x', 1)"),
        ("item current_rev NOT NULL", "INSERT INTO items (id, kind, scope_kind, owner_id, key, current_rev, created_at) VALUES ('X', 'note', 'private', 'p1', 'x', NULL, 1)"),
        # item_revisions
        ("rev lower bound", "INSERT INTO item_revisions (item_id, rev, state, valid_from, actor_id, recorded_at) VALUES ('I1', 0, 'candidate', 1, 'p1', 1)"),
        ("rev upper bound", "INSERT INTO item_revisions (item_id, rev, state, valid_from, actor_id, recorded_at) VALUES ('I1', 10000000, 'candidate', 1, 'p1', 1)"),
        ("revision state", "INSERT INTO item_revisions (item_id, rev, state, valid_from, actor_id, recorded_at) VALUES ('I1', 2, 'superseded', 1, 'p1', 1)"),
        ("valid_to > valid_from", "INSERT INTO item_revisions (item_id, rev, state, valid_from, valid_to, actor_id, recorded_at) VALUES ('I1', 2, 'candidate', 5, 5, 'p1', 1)"),
        ("accepted needs accepted_at", "INSERT INTO item_revisions (item_id, rev, state, valid_from, actor_id, recorded_at) VALUES ('I1', 2, 'accepted', 1, 'p1', 1)"),
        ("candidate has no accepted_at", "INSERT INTO item_revisions (item_id, rev, state, valid_from, actor_id, recorded_at, accepted_at) VALUES ('I1', 2, 'candidate', 1, 'p1', 1, 1)"),
        ("revision PK", "INSERT INTO item_revisions (item_id, rev, state, valid_from, actor_id, recorded_at, accepted_at) VALUES ('I1', 1, 'accepted', 1, 'p1', 1, 1)"),
        ("revision item FK", "INSERT INTO item_revisions (item_id, rev, state, valid_from, actor_id, recorded_at) VALUES ('nope', 1, 'candidate', 1, 'p1', 1)"),
        ("valid_from NOT NULL", "INSERT INTO item_revisions (item_id, rev, state, actor_id, recorded_at) VALUES ('I1', 2, 'candidate', 'p1', 1)"),
        ("actor NOT NULL", "INSERT INTO item_revisions (item_id, rev, state, valid_from, recorded_at) VALUES ('I1', 2, 'candidate', 1, 1)"),
        ("recorded_at NOT NULL", "INSERT INTO item_revisions (item_id, rev, state, valid_from, actor_id) VALUES ('I1', 2, 'candidate', 1, 'p1')"),
        # fact_versions
        ("fact op", "INSERT INTO fact_versions (item_id, rev, op) VALUES ('I1', 1, 'delete')"),
        ("fact op NOT NULL", "INSERT INTO fact_versions (item_id, rev) VALUES ('I1', 1)"),
        ("fact revision FK", "INSERT INTO fact_versions (item_id, rev, op) VALUES ('I1', 9, 'assert')"),
        # sources, provenance, extraction
        ("source status", "INSERT INTO sources (id, adapter, content_hash, captured_at, status) VALUES ('S2', 'md', x'00', 1, 'gone')"),
        ("source content_hash NOT NULL", "INSERT INTO sources (id, adapter, captured_at, status) VALUES ('S2', 'md', 1, 'live')"),
        ("provenance confidence", "INSERT INTO provenance (item_id, rev, source_id, confidence) VALUES ('I1', 1, 'S1', 1.5)"),
        ("provenance source FK", "INSERT INTO provenance (item_id, rev, source_id) VALUES ('I1', 1, 'nope')"),
        ("provenance revision FK", "INSERT INTO provenance (item_id, rev, source_id) VALUES ('I1', 7, 'S1')"),
        ("provenance run FK", "INSERT INTO provenance (item_id, rev, source_id, run_id) VALUES ('I1', 1, 'S1', 'nope')"),
        ("run status", "INSERT INTO extraction_runs (id, source_id, model_id, endpoint, manifest_json, manifest_hash, started_at, status) VALUES ('R2', 'S1', 'm', 'e', '{}', x'00', 1, 'paused')"),
        ("run source FK", "INSERT INTO extraction_runs (id, source_id, model_id, endpoint, manifest_json, manifest_hash, started_at, status) VALUES ('R2', 'nope', 'm', 'e', '{}', x'00', 1, 'done')"),
        ("chunk status", "INSERT INTO extraction_chunks VALUES (x'01', 'm', 't', 'R1', 'pending')"),
        ("chunk run FK", "INSERT INTO extraction_chunks VALUES (x'01', 'm', 't', 'nope', 'done')"),
        # conflicts, flags, pins, receipts, audit, artifacts, meta, erasures, embeddings, v1_map
        ("conflict status", "INSERT INTO conflicts (id, item_id, rev_a, rev_b, status) VALUES ('C', 'I1', 1, 2, 'ignored')"),
        ("conflict item FK", "INSERT INTO conflicts (id, item_id, rev_a, rev_b, status) VALUES ('C', 'nope', 1, 2, 'open')"),
        ("flag type", "INSERT INTO flags VALUES ('I1', 1, 'spam', 'I2', 1)"),
        ("flag item FK", "INSERT INTO flags VALUES ('nope', 1, 'dedupe', 'I2', 1)"),
        ("pin item FK", "INSERT INTO pins VALUES ('p1', 'nope')"),
        ("pin principal FK", "INSERT INTO pins VALUES ('nobody', 'I1')"),
        ("receipt scheme", "INSERT INTO receipts (principal_id, request_id, fingerprint, scheme, recorded_at) VALUES ('p1', 'r', x'00', 3, 1)"),
        ("receipt fingerprint NOT NULL", "INSERT INTO receipts (principal_id, request_id, scheme, recorded_at) VALUES ('p1', 'r', 2, 1)"),
        ("audit prev_hash NOT NULL", "INSERT INTO audit (at, actor, action, hash) VALUES (1, 'a', 'b', x'01')"),
        ("artifact kind", "INSERT INTO artifacts VALUES ('/x', 'snapshot', 1, 2)"),
        ("meta value NOT NULL", "INSERT INTO meta (name) VALUES ('x')"),
        ("erasure rev NOT NULL", "INSERT INTO erasures (item_id, at) VALUES ('I1', 1)"),
        ("embedding vector NOT NULL", "INSERT INTO embeddings (item_id, rev, model, dim) VALUES ('I1', 1, 'm', 3)"),
        ("v1_map item NOT NULL", "INSERT INTO v1_map (v1_ref) VALUES ('x')"),
    ]

    def test_violations_are_rejected(self):
        for name, sql, *params in self.REJECTED:
            with self.subTest(name):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(sql, *params)

    def test_valid_rows_are_accepted(self):
        # Sanity: the same shapes with valid values succeed, so the rejections above are the constraints.
        x = self.db.execute
        x("INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at, deleted_at) VALUES ('X', 'note', 'private', 'p1', NULL, 1, 2)")
        x("INSERT INTO items (id, kind, scope_kind, owner_id, key, created_at, deleted_at) VALUES ('Y', 'note', 'private', 'p1', NULL, 1, 2)")
        x("INSERT INTO items (id, kind, scope_kind, owner_id, project_id, key, created_at) VALUES ('Z', 'note', 'project', 'p1', 'proj', 'k', 1)")
        x("INSERT INTO item_revisions (item_id, rev, state, valid_from, valid_to, actor_id, recorded_at) VALUES ('I1', 9999999, 'candidate', 5, 6, 'p1', 1)")
        x("INSERT INTO provenance (item_id, rev, source_id, confidence) VALUES ('I1', 1, 'S1', 1.0)")
        x("INSERT INTO receipts (principal_id, request_id, fingerprint, scheme, recorded_at) VALUES ('p1', 'r', x'00', 1, 1)")
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_immutability_triggers(self):
        x = self.db.execute
        for sql in ("UPDATE item_revisions SET body = 'changed' WHERE item_id = 'I1'",
                    "UPDATE item_revisions SET valid_to = 99 WHERE item_id = 'I1'",
                    "UPDATE item_revisions SET state = 'candidate', accepted_at = NULL WHERE item_id = 'I1'",
                    "UPDATE item_revisions SET accepted_at = 2 WHERE item_id = 'I1'"):
            with self.subTest(sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    x(sql)
        x("UPDATE item_revisions SET body = NULL WHERE item_id = 'I1'")   # deletion may null content
        x("UPDATE item_revisions SET state = 'accepted', accepted_at = 3, reviewed_by = '~operator', reviewed_at = 3"
          " WHERE item_id = 'I2'")
        x("INSERT INTO fact_versions (item_id, rev, op, value_json) VALUES ('I1', 1, 'assert', '1')")
        with self.assertRaises(sqlite3.IntegrityError):
            x("UPDATE fact_versions SET value_json = '2'")
        with self.assertRaises(sqlite3.IntegrityError):
            x("UPDATE fact_versions SET op = 'retract'")


class StoreFileTests(StoreTestCase):
    def test_pragmas_and_strict_tables(self):
        store = self.new_store()
        db = store.db
        self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], USER_VERSION)
        tables = {row["name"]: row["strict"] for row in db.execute("PRAGMA table_list")
                  if row["schema"] == "main" and row["type"] == "table"
                  and not row["name"].startswith(("sqlite_", "fts_items"))}
        self.assertEqual(len(tables), 22)
        self.assertTrue(all(tables.values()), tables)
        self.assertEqual(db.execute("SELECT value FROM meta WHERE name = 'estimator'").fetchone()[0], "bytes3-v1")

    def test_old_sqlite_is_refused(self):
        directory = self.private_dir()
        with mock.patch.object(connection.sqlite3, "sqlite_version_info", (3, 36, 0)):
            self.assertCode("storage_unavailable", Store.open, directory / "s.sqlite3", create=True)
        self.assertFalse((directory / "s.sqlite3").exists())

    def test_newer_and_foreign_stores_are_refused_with_zero_writes(self):
        directory = self.private_dir()
        newer = directory / "newer.sqlite3"
        store = Store.open(newer, create=True)
        store.db.execute(f"PRAGMA user_version = {USER_VERSION + 1}")
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.close()
        before = sha256(newer.read_bytes()).hexdigest()
        self.assertCode("schema_version", Store.open, newer)
        self.assertEqual(sha256(newer.read_bytes()).hexdigest(), before)

        foreign = directory / "foreign.sqlite3"
        db = sqlite3.connect(foreign)
        db.execute("CREATE TABLE t (x)")
        db.close()
        foreign.chmod(0o600)
        self.assertCode("wrong_store", Store.open, foreign)

    def test_unsafe_modes_are_refused(self):
        directory = self.private_dir()
        path = directory / "s.sqlite3"
        Store.open(path, create=True).close()
        path.chmod(0o644)
        self.assertCode("unsafe_path", Store.open, path)
        path.chmod(0o600)
        directory.chmod(0o755)
        self.assertCode("unsafe_path", Store.open, path)
        directory.chmod(0o700)
        Store.open(path).close()


if __name__ == "__main__":
    unittest.main()
