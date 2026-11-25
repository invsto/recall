"""v1 import: counts, foreign keys, untouched v1 bytes, mapping report, refusal cases (spec 7.9)."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import unittest

from recall_v2.core.errors import RecallError
from recall_v2.migrate import migrate_v1
from recall_v2.store.schema import USER_VERSION
from recall_v2.migrate.v1 import report_path, v1_ref
from recall_v2.store import Store, audit

from .helpers import StoreTestCase, StepClock, T0

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "store_v2.sqlite3"
FIXTURE_TOKEN = "recall-fixture-token-alpha"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def query(path: Path, sql: str, params=()):
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


class FixtureMigrationTests(StoreTestCase):
    def setUp(self):
        self.out_dir = self.private_dir()
        self.dest = self.out_dir / "v2.sqlite3"
        self.fixture_files = sorted(p.name for p in FIXTURE.parent.iterdir())

    def test_fixture_imports_with_matching_counts_and_untouched_bytes(self):
        before = digest(FIXTURE)
        report = migrate_v1(FIXTURE, self.dest, clock=StepClock())
        self.assertEqual(digest(FIXTURE), before, "the v1 file must not change")
        self.assertEqual(sorted(p.name for p in FIXTURE.parent.iterdir()), self.fixture_files,
                         "no sidecar files beside the v1 store")
        self.assertTrue(report["v1_unchanged"])
        self.assertEqual(report["source_user_version"], 2)
        self.assertEqual(report["unmapped"], [])
        expected = {"principals": 2, "projects": 1, "memberships": 1, "checkpoints": 0, "checkpoint_revisions": 0,
                    "proposals": 0, "publications": 0, "receipts": 0, "audit": 4}
        self.assertEqual({k: (v["in"], v["out"]) for k, v in report["tables"].items()},
                         {k: (n, n) for k, n in expected.items()})
        self.assertEqual(report["checks"]["foreign_key_check"], [])
        self.assertEqual(report["checks"]["integrity_check"], "ok")

        # The mapping report is written beside the new file, 0600, and matches the return value.
        written = report_path(self.dest)
        self.assertEqual(json.loads(written.read_text()), json.loads(json.dumps(report, default=str)))
        self.assertEqual(oct(os.stat(written).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.dest).st_mode & 0o777), "0o600")
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()),
                         ["v2.sqlite3", "v2.sqlite3.migration-report.json"])

        self.assertEqual(query(self.dest, "PRAGMA foreign_key_check"), [])
        self.assertEqual(query(self.dest, "PRAGMA user_version"), [(USER_VERSION,)])
        self.assertEqual(query(self.dest, "SELECT id, revoked_at IS NULL FROM principals ORDER BY id"),
                         [("alpha", 1), ("beta", 1)])
        self.assertEqual(query(self.dest, "SELECT principal_id, project_id FROM memberships"), [("alpha", "default")])
        self.assertEqual(query(self.dest, "SELECT actor, action, item_id, project_id FROM audit WHERE legacy = 1 ORDER BY id"),
                         [("operator", "bot-add", "alpha", None), ("operator", "bot-add", "beta", None),
                          ("operator", "project-add", None, "default"),
                          ("operator", "member-grant", "alpha", "default")])

        store = Store.open(self.dest)
        self.addCleanup(store.close)
        self.assertTrue(store.verify_audit()["ok"])
        alpha = store.authenticate(FIXTURE_TOKEN)   # the v1 credential digest carries over
        self.assertEqual(alpha.id, "alpha")
        out = store.save(alpha, kind="note", request_id="after", key="n", body="x", scope="project", project="default")
        self.assertEqual(out["rev"], 1)

    def test_too_new_store_is_refused_with_zero_writes(self):
        work = self.private_dir()
        newer = work / "newer.sqlite3"
        shutil.copyfile(FIXTURE, newer)
        db = sqlite3.connect(newer)
        db.execute("PRAGMA user_version = 4")
        db.close()
        before = digest(newer)
        self.assertCode("schema_version", migrate_v1, newer, self.dest)
        self.assertEqual(digest(newer), before)
        self.assertEqual(list(self.out_dir.iterdir()), [])

    def test_wrong_store_destination_exists_and_dry_run(self):
        junk = self.private_dir() / "junk.sqlite3"
        junk.write_bytes(b"not a database at all" * 100)
        self.assertCode("wrong_store", migrate_v1, junk, self.dest)
        self.assertEqual(list(self.out_dir.iterdir()), [])

        report = migrate_v1(FIXTURE, self.dest, dry_run=True)
        self.assertEqual(report["unmapped"], [])
        self.assertEqual(list(self.out_dir.iterdir()), [], "dry run writes nothing")

        migrate_v1(FIXTURE, self.dest)
        before = digest(self.dest)
        self.assertCode("backup_exists", migrate_v1, FIXTURE, self.dest)
        self.assertEqual(digest(self.dest), before)

        # A v2 store is not a v1 source.
        self.assertCode("wrong_store", migrate_v1, self.dest, self.out_dir / "again.sqlite3")


class RichMigrationTests(StoreTestCase):
    """A v1 store at user_version 3 built through the v1 code, with every row type and edge case."""

    def build_v3(self) -> tuple[Path, dict]:
        from recall_core.service.store import ServiceStore, credential_digest
        directory = self.private_dir()
        path = directory / "v1.sqlite3"
        v1 = ServiceStore(path)
        v1.initialize(create=True)
        alpha = credential_digest(v1.change_credential("alpha", "principal-add")["token"])
        beta = credential_digest(v1.change_credential("beta", "principal-add")["token"])
        v1.change_credential("gamma", "principal-add")
        v1.change_credential("gamma", "principal-revoke")
        v1.project_add("proj")
        v1.project_add("empty")
        v1.change_membership("proj", "alpha", grant=True)
        v1.change_membership("proj", "beta", grant=True)

        v1.checkpoint_save(alpha, "task", "migrate-db", 0, "c1", {"step": 1})
        v1.checkpoint_save(alpha, "task", "migrate-db", 1, "c2", {"step": 2})
        v1.checkpoint_save(beta, "project", "proj", 0, "c3", {"note": "b"})
        v1.checkpoint_save(alpha, "task", "erase-me", 0, "c4", {"secret": "gone"})
        v1.delete_checkpoint("alpha", "task", "erase-me")

        ex = lambda text: {"excerpt": text, "source_ref": "https://example.test/x"}
        p1 = v1.publication_propose(alpha, "excerpt", "proj", "ex-a", 0, "p1", ex("first"))["proposal_id"]
        v1.review(p1, "approve", "owner")
        p2 = v1.publication_propose(beta, "excerpt", "proj", "ex-a", 1, "p2", ex("second"))["proposal_id"]
        v1.review(p2, "approve", "owner")
        p3 = v1.publication_propose(alpha, "excerpt", "proj", "ex-a", 2, "p3", ex("rejected"))["proposal_id"]
        v1.review(p3, "reject", "owner")
        v1.publication_propose(beta, "excerpt", "proj", "ex-a", 2, "p4", ex("pending"))
        p5 = v1.publication_propose(alpha, "brief", "proj", "main", 0, "p5", {"markdown": "# Brief\n\nText"})["proposal_id"]
        v1.review(p5, "approve", "owner")
        p6 = v1.publication_propose(alpha, "excerpt", "proj", "ex-b", 0, "p6", ex("removed"))["proposal_id"]
        v1.review(p6, "approve", "owner")
        v1.delete_publication("proj", "excerpt", "ex-b")
        v1.publication_propose(beta, "excerpt", "proj", "ex-b", 2, "p7", ex("after removal"))
        return path, {"alpha": alpha}

    def test_v3_store_maps_every_row(self):
        source, _ = self.build_v3()
        before = digest(source)
        counts_v1 = {t: query(source, f"SELECT COUNT(*) FROM {t}")[0][0] for t in
                     ("principals", "projects", "memberships", "checkpoints", "checkpoint_revisions", "receipts",
                      "audit", "proposals", "publications")}
        dest = self.private_dir() / "v2.sqlite3"
        report = migrate_v1(source, dest, clock=StepClock())
        self.assertEqual(digest(source), before)
        self.assertEqual(report["source_user_version"], 3)
        self.assertEqual(report["unmapped"], [])
        for table, count in counts_v1.items():
            self.assertEqual(report["tables"][table]["in"], count, table)
            self.assertEqual(report["tables"][table]["out"], count, table)
        self.assertEqual(counts_v1["proposals"], 6)   # p6 was deleted by the v1 removal
        self.assertEqual(query(dest, "PRAGMA foreign_key_check"), [])
        self.assertEqual(query(dest, "PRAGMA integrity_check"), [("ok",)])

        # Principals: revoked credential -> revoked_at set.
        self.assertEqual(query(dest, "SELECT id, token_digest IS NULL, revoked_at IS NOT NULL FROM principals ORDER BY id"),
                         [("alpha", 0, 0), ("beta", 0, 0), ("gamma", 1, 1)])
        # Checkpoints: live item with two accepted revisions; erased checkpoint is a tombstone without content.
        rows = query(dest, "SELECT key, current_rev, deleted_at IS NOT NULL FROM items WHERE kind = 'checkpoint' ORDER BY owner_id, key")
        self.assertIn(("task:migrate-db", 2, 0), rows)
        self.assertIn((None, 2, 1), rows)
        self.assertEqual(query(dest, "SELECT body FROM item_revisions AS r JOIN items AS i ON i.id = r.item_id"
                                     " WHERE i.key = 'task:migrate-db' ORDER BY rev"),
                         [('{"step":1}',), ('{"step":2}',)])
        self.assertEqual(query(dest, "SELECT COUNT(*) FROM item_revisions WHERE body LIKE '%gone%'"), [(0,)])
        # Receipts: scheme 1, pointing at the checkpoint items.
        self.assertEqual(query(dest, "SELECT COUNT(*) FROM receipts AS r JOIN items AS i ON i.id = r.item_id"
                                     " WHERE r.scheme = 1"), [(3,)])
        # Proposals: accepted revs keep their number; rejected/pending take the next free revs in submitted order.
        ex_a = query(dest, "SELECT r.rev, r.state, r.body, r.reviewed_by FROM item_revisions AS r JOIN items AS i"
                           " ON i.id = r.item_id WHERE i.key = 'ex-a' ORDER BY r.rev")
        self.assertEqual(ex_a, [(1, "accepted", "first", "owner"), (2, "accepted", "second", "owner"),
                                (3, "rejected", "rejected", "owner"), (4, "candidate", "pending", None)])
        self.assertEqual(query(dest, "SELECT current_rev, owner_id FROM items WHERE key = 'ex-a'"), [(2, "alpha")])
        # Removed publication -> tombstone; a proposal submitted after the removal -> a new live item.
        ex_b = query(dest, "SELECT i.key, i.deleted_at IS NOT NULL, i.current_rev, COUNT(r.rev) FROM items AS i"
                           " LEFT JOIN item_revisions AS r ON r.item_id = i.id WHERE i.kind = 'excerpt'"
                           " AND (i.key = 'ex-b' OR i.key IS NULL) GROUP BY i.id ORDER BY i.key")
        self.assertEqual(sorted(ex_b, key=str), sorted([(None, 1, 2, 0), ("ex-b", 0, 0, 1)], key=str))
        # Every v1 reference is in v1_map.
        refs = {row[0] for row in query(dest, "SELECT v1_ref FROM v1_map")}
        self.assertIn(v1_ref("checkpoints", "alpha", "task", "migrate-db"), refs)
        self.assertEqual(sum(1 for r in refs if r.startswith('["proposals"')), counts_v1["proposals"])
        self.assertEqual(sum(1 for r in refs if r.startswith('["publications"')), counts_v1["publications"])
        # Legacy audit rows are chained; the store opens and verifies.
        store = Store.open(dest)
        self.addCleanup(store.close)
        self.assertTrue(store.verify_audit()["ok"])
        self.assertEqual(query(dest, "SELECT COUNT(*) FROM audit WHERE legacy = 1")[0][0], counts_v1["audit"])
        # FTS covers exactly the current accepted revision of each live non-fact item.
        self.assertEqual(query(dest, "SELECT r.body FROM item_revisions AS r JOIN fts_items AS f ON f.rowid = r.rowid"
                                     " ORDER BY r.body"),
                         [("# Brief\n\nText",), ("second",), ('{"note":"b"}',), ('{"step":2}',)])

    def test_v2_store_with_rows_imports(self):
        """The user_version 2 layout (bot columns), enriched with direct inserts on a copy of the fixture."""
        work = self.private_dir()
        source = work / "v1.sqlite3"
        shutil.copyfile(FIXTURE, source)
        db = sqlite3.connect(source)
        ts = "2026-11-24T18:00:00.000000+00:00"
        db.executescript(f"""
            INSERT INTO checkpoints VALUES ('alpha', 'task', 'k', 1, '{ts}');
            INSERT INTO checkpoint_revisions VALUES ('alpha', 'task', 'k', 1, '{ts}', '{{"a":1}}');
            INSERT INTO receipts VALUES ('alpha', 'req', x'00', 'task', 'k', 1);
            INSERT INTO proposals (id, project, kind, key, author, expected_revision, request_id, fingerprint, content,
                                   submitted, status, reviewer, reviewed, revision)
                VALUES ('p1', 'default', 'brief', 'main', 'alpha', 0, 'r1', x'00', '{{"markdown":"# B"}}',
                        '{ts}', 'accepted', 'owner', '{ts}', 1);
            INSERT INTO publications VALUES ('default', 'brief', 'main', 1, 'p1');
        """)
        db.close()
        before = digest(source)
        dest = self.private_dir() / "v2.sqlite3"
        report = migrate_v1(source, dest)
        self.assertEqual(digest(source), before)
        self.assertEqual(report["unmapped"], [])
        self.assertEqual({k: v["out"] for k, v in report["tables"].items()},
                         {"principals": 2, "projects": 1, "memberships": 1, "checkpoints": 1,
                          "checkpoint_revisions": 1, "proposals": 1, "publications": 1, "receipts": 1, "audit": 4})
        self.assertEqual(query(dest, "PRAGMA foreign_key_check"), [])
        store = Store.open(dest)
        self.addCleanup(store.close)
        alpha = store.authenticate(FIXTURE_TOKEN)
        item_id = query(dest, "SELECT id FROM items WHERE kind = 'brief'")[0][0]
        got = store.get(alpha, item_id=item_id)
        self.assertEqual((got["body"], got["state"], got["rev"], got["key"]), ("# B", "accepted", 1, "main"))
        self.assertEqual(got["accepted_at"], "2026-11-24T18:00:00.000000Z")

    def test_unmappable_rows_fail_the_migration(self):
        work = self.private_dir()
        source = work / "v1.sqlite3"
        shutil.copyfile(FIXTURE, source)
        db = sqlite3.connect(source)   # foreign keys off: an orphan revision v1 never wrote
        db.execute("INSERT INTO checkpoint_revisions VALUES ('alpha', 'task', 'orphan', 1, "
                   "'2026-11-24T18:00:00+00:00', '{}')")
        db.commit()
        db.close()
        out = self.private_dir()
        error = self.assertCode("migration_failed", migrate_v1, source, out / "v2.sqlite3")
        self.assertEqual(error.details["report"]["unmapped"][0]["table"], "checkpoint_revisions")
        self.assertEqual(list(out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
