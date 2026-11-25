"""`doctor`: a read-only health report of the store, operator only.

Reports, in this key order: schema (application ID, user_version, envelope schema), SQLite version,
integrity_check, foreign_key_check violations, audit chain status, candidate counts (open and stale),
open conflicts, live and deleted item counts, erasures and registered artifacts. Every value is
JSON-serialisable. It runs in one read transaction and writes nothing.
"""
from __future__ import annotations

import sqlite3

from .. import SCHEMA_VERSION
from ..core import timefmt
from ..governance.authority import require_operator
from ..governance.review import STALE_AFTER_MICROS
from ..store import audit
from ..store.connection import read_transaction
from ..store.schema import APPLICATION_ID, USER_VERSION


def doctor(session) -> dict:
    store = require_operator(session).store
    with read_transaction(store.db) as db:
        now = store.clock()
        integrity = [row[0] for row in db.execute("PRAGMA integrity_check")]
        fk = db.execute("PRAGMA foreign_key_check").fetchall()
        chain = audit.verify_chain(db)
        candidates = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(r.recorded_at < ?), 0) FROM item_revisions AS r JOIN items AS i"
            " ON i.id = r.item_id WHERE r.state = 'candidate' AND i.deleted_at IS NULL",
            (now - STALE_AFTER_MICROS,)).fetchone()
        conflicts = [{"id": r["id"], "item_id": r["item_id"], "rev_a": r["rev_a"], "rev_b": r["rev_b"]}
                     for r in db.execute("SELECT id, item_id, rev_a, rev_b FROM conflicts WHERE status = 'open'"
                                         " ORDER BY id")]
        items = db.execute("SELECT COALESCE(SUM(deleted_at IS NULL), 0), COALESCE(SUM(deleted_at IS NOT NULL), 0)"
                           " FROM items").fetchone()
        artifacts = {kind: count for kind, count in db.execute(
            "SELECT kind, COUNT(*) FROM artifacts GROUP BY kind ORDER BY kind")}
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        erasures = db.execute("SELECT COUNT(*) FROM erasures").fetchone()[0]
        anchored = db.execute("SELECT 1 FROM meta WHERE name = ?", (audit.ANCHOR_NAME,)).fetchone() is not None
    healthy = integrity == ["ok"] and not fk and chain["ok"] and app_id == APPLICATION_ID and version == USER_VERSION
    return {
        "ok": healthy,
        "checked_at": timefmt.format_rfc3339(now),
        "schema": {"application_id": app_id, "user_version": version, "expected_user_version": USER_VERSION,
                   "envelope": SCHEMA_VERSION},
        "sqlite_version": sqlite3.sqlite_version,
        "integrity_check": "ok" if integrity == ["ok"] else integrity,
        "foreign_key_violations": len(fk),
        "audit": {"ok": chain["ok"], "checked": chain["checked"], "first_bad_id": chain["first_bad_id"],
                  "anchored": anchored},
        "candidates": {"open": candidates[0], "stale": candidates[1],
                       "stale_after_days": STALE_AFTER_MICROS // timefmt.MICROS_PER_DAY},
        "conflicts": {"open": len(conflicts), "items": conflicts},
        "items": {"live": items[0], "deleted": items[1]},
        "erasures": erasures,
        "artifacts": {"backup": artifacts.get("backup", 0), "export": artifacts.get("export", 0),
                      "migration": artifacts.get("migration", 0)},
    }
