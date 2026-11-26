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
