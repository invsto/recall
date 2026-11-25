"""Append-only, content-free, hash-chained audit log.

hash = SHA-256(prev_hash || canonical_json(row)); genesis prev_hash is 32 zero bytes; after pruning,
meta.audit_anchor (hex) holds the last pruned hash and verification starts from it.
"""
from __future__ import annotations

from hashlib import sha256
import sqlite3
from typing import Optional

from ..core.normalise import canonical_json

GENESIS = bytes(32)
ANCHOR_NAME = "audit_anchor"

# Actions this build writes. Legacy (imported) rows keep their original action names.
ACTIONS = frozenset({
    "principal_add", "principal_rotate", "principal_revoke", "project_add", "member_add", "member_remove",
    "save", "retract", "review_approve", "review_reject", "review_edit", "conflict_resolve", "delete", "migrate",
    "pin", "unpin",
    "source_add", "source_update", "extract_plan", "extract_ack", "extract_start", "extract_chunk",
    "extract_cancel", "extract_end",
    "expire", "source_delete", "backup", "backup_prune", "restore", "export", "export_prune", "retention_prune",
})


def row_digest(prev_hash: bytes, row: dict) -> bytes:
    payload = canonical_json({
        "id": row["id"], "at": row["at"], "actor": row["actor"], "action": row["action"],
        "item_id": row["item_id"], "rev": row["rev"], "project_id": row["project_id"],
        "legacy": row["legacy"],
    })
    return sha256(prev_hash + payload.encode("utf-8")).digest()


def _anchor(db: sqlite3.Connection) -> bytes:
    row = db.execute("SELECT value FROM meta WHERE name = ?", (ANCHOR_NAME,)).fetchone()
    return bytes.fromhex(row[0]) if row else GENESIS


def append(db: sqlite3.Connection, *, at: int, actor: str, action: str, item_id: Optional[str] = None,
           rev: Optional[int] = None, project_id: Optional[str] = None, legacy: int = 0) -> int:
    """Append one event inside the caller's transaction. Events carry identifiers only, never content."""
    if not legacy and action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    last = db.execute("SELECT id, hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = last[1] if last else _anchor(db)
    row = {"id": (last[0] + 1) if last else 1, "at": at, "actor": actor, "action": action,
           "item_id": item_id, "rev": rev, "project_id": project_id, "legacy": legacy}
    digest = row_digest(prev_hash, row)
    db.execute(
        "INSERT INTO audit (id, at, actor, action, item_id, rev, project_id, legacy, prev_hash, hash)"
        " VALUES (:id, :at, :actor, :action, :item_id, :rev, :project_id, :legacy, :prev_hash, :hash)",
        {**row, "prev_hash": prev_hash, "hash": digest},
    )
    return row["id"]


def verify_chain(db: sqlite3.Connection) -> dict:
    """Recompute every hash from the anchor. Returns {"ok", "checked", "first_bad_id"}."""
    expected_prev = _anchor(db)
    checked = 0
    previous_id = None
    for row in db.execute(
        "SELECT id, at, actor, action, item_id, rev, project_id, legacy, prev_hash, hash FROM audit ORDER BY id"
    ):
        row = dict(zip(("id", "at", "actor", "action", "item_id", "rev", "project_id", "legacy",
                        "prev_hash", "hash"), tuple(row)))
        good = (
            (previous_id is None or row["id"] == previous_id + 1)
            and row["prev_hash"] == expected_prev
            and row["hash"] == row_digest(expected_prev, row)
        )
        if not good:
            return {"ok": False, "checked": checked, "first_bad_id": row["id"]}
        expected_prev = row["hash"]
        previous_id = row["id"]
        checked += 1
    return {"ok": True, "checked": checked, "first_bad_id": None}
