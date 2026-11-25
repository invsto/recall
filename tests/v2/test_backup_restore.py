"""Backups and restore (spec 6): online backup into a new 0600 file in a 0700 directory, integrity check,
token digests cleared, audit chain included; restore only to a new path with ledger replay; pruning after
30 days; and the documented limit that content deleted after a backup stays in it until it is pruned."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat
import unittest

from recall_v2.api import Client, Operator
from recall_v2.core import timefmt
from recall_v2.core.errors import RecallError
from recall_v2.lifecycle import backup as backup_module
from recall_v2.store import Store, audit

from .governance_helpers import GovernanceCase

DAY = timefmt.MICROS_PER_DAY
CANARY = "zqxbackupcanary4411"


class BackupRestoreTests(GovernanceCase):
    def setUp(self):
        super().setUp()
        self.secret = self.ok(self.alice.save(kind="note", request_id="s1", key="vault", title="vault",
                                              body=f"the code is {CANARY}"))
        self.keep = self.ok(self.alice.save(kind="note", request_id="s2", key="keep", body="ordinary otter note"))

    def test_backup_file_mode_integrity_and_audit_chain(self):
        made = self.ok(self.op.backup())
        path = Path(made["path"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(path.parent, self.path.parent / "backups")
        self.assertEqual(made["integrity"], "ok")
        self.assertTrue(made["audit"]["ok"])
        copy = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM principals WHERE token_digest IS NOT NULL").fetchone()[0], 0)
            chain = audit.verify_chain(copy)
            self.assertTrue(chain["ok"])
            self.assertGreater(chain["checked"], 5)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM audit").fetchone()[0], chain["checked"])
        finally:
            copy.close()
        row = self.store.db.execute("SELECT * FROM artifacts WHERE path = ?", (str(path),)).fetchone()
        self.assertEqual((row["kind"], row["expires_at"] - row["created_at"]), ("backup", 30 * DAY))
        self.assertEqual(self.store.db.execute("SELECT action FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0], "backup")

    def test_backup_refuses_existing_path_and_unsafe_directory(self):
        target = self.private_dir() / "b.sqlite3"
        target.write_bytes(b"x")
        self.err(self.op.backup(path=str(target)), "backup_exists")
        self.assertEqual(target.read_bytes(), b"x")
        loose = self.private_dir() / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        self.err(self.op.backup(path=str(loose / "b.sqlite3")), "unsafe_path")

    def test_deleted_record_stays_in_old_backup_until_pruned(self):
        made = self.ok(self.op.backup())
        path = Path(made["path"])
        self.ok(self.op.delete(self.secret["id"]))
        self.ok(self.op.maintain())
        self.assertNotIn(CANARY.encode(), self.path.read_bytes())       # gone from the live store
        self.assertIn(CANARY.encode(), path.read_bytes())               # the documented limit
        self.assertEqual(self.ok(self.op.backup_prune())["pruned"], [])  # not yet expired
        self.assertTrue(path.exists())
        self.clock.now += 30 * DAY
        pruned = self.ok(self.op.backup_prune())["pruned"]
        self.assertEqual(pruned, [{"path": str(path), "kind": "backup"}])
        self.assertFalse(path.exists())
        self.assertEqual(self.count("artifacts", "path = ?", (str(path),)), 0)
        found = [p for p in path.parent.iterdir() if CANARY.encode() in p.read_bytes()]
        self.assertEqual(found, [])

    def test_restore_replays_the_ledger(self):
        made = self.ok(self.op.backup())
        self.ok(self.op.delete(self.secret["id"]))
        target = self.private_dir() / "restored.sqlite3"
        out = self.ok(self.op.restore(made["path"], to=str(target)))
        self.assertEqual((out["integrity"], out["replayed"], out["ledger_replayed"]), ("ok", 1, True))
        self.assertTrue(out["audit"]["ok"])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotIn(CANARY.encode(), target.read_bytes())
        restored = Store.open(target)
        try:
            op = Operator.open(restored)
            self.assertTrue(self.ok(op.audit_verify())["ok"])
            row = restored.db.execute("SELECT deleted_at, key FROM items WHERE id = ?", (self.secret["id"],)).fetchone()
            self.assertIsNotNone(row["deleted_at"])
            self.assertIsNone(row["key"])
            body = restored.db.execute("SELECT body FROM item_revisions WHERE item_id = ?", (self.keep["id"],)).fetchone()
            self.assertEqual(body[0], "ordinary otter note")
            # token digests were cleared: old tokens fail until the principal is rotated
            stale = Client.open(restored, self.tokens["alice"])
            self.err(stale.lookup(query="otter"), "unauthorized")
            fresh = Client.open(restored, self.ok(op.principal_rotate("alice"))["token"])
            self.assertEqual([r["id"] for r in self.results(fresh.lookup(query="otter"))], [self.keep["id"]])
        finally:
            restored.close()
        self.assertEqual(self.store.db.execute("SELECT action FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0],
                         "restore")

    def test_restore_without_ledger_keeps_the_backup_content(self):
        made = self.ok(self.op.backup())
        self.ok(self.op.delete(self.secret["id"]))
        self.store.ledger_path.unlink()
        target = self.private_dir() / "r.sqlite3"
        self.err(self.op.restore(made["path"], to=str(target)), "invalid_input")
        self.assertFalse(target.exists())
        out = self.ok(self.op.restore(made["path"], to=str(target), no_ledger=True))
        self.assertEqual((out["ledger_replayed"], out["replayed"]), (False, 0))
        self.assertIn(CANARY.encode(), target.read_bytes())

    def test_restore_refuses_an_existing_path(self):
        made = self.ok(self.op.backup())
        target = self.private_dir() / "exists.sqlite3"
        target.write_bytes(b"keep me")
        self.err(self.op.restore(made["path"], to=str(target)), "backup_exists")
        self.assertEqual(target.read_bytes(), b"keep me")
        self.err(self.op.restore(made["path"], to=str(self.path)), "backup_exists")   # never over the live store
        self.err(self.op.restore(made["path"], to=made["path"]), "backup_exists")

    def test_restore_refuses_a_foreign_file(self):
        foreign = self.private_dir() / "foreign.sqlite3"
        sqlite3.connect(foreign).close()
        target = self.private_dir() / "r.sqlite3"
        self.err(self.op.restore(str(foreign), to=str(target), no_ledger=True), "wrong_store")
        self.assertFalse(target.exists())

    def test_operator_only(self):
        for target in (self.alice, self.store, None):
            for func, args in ((backup_module.backup, ()), (backup_module.backup_prune, ()),
                               (backup_module.restore, ("a", "b"))):
                with self.assertRaises(RecallError) as raised:
                    func(target, *args)
                self.assertEqual(raised.exception.code, "forbidden")
        for name in ("backup", "restore", "backup_prune"):
            self.assertFalse(hasattr(self.alice, name))


if __name__ == "__main__":
    unittest.main()
