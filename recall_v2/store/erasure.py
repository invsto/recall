"""Erasure of one item (section 6, Deletion) and of single revision bodies (retention, source deletion).

Erasing an item, inside the caller's IMMEDIATE transaction:
- every revision's title, body, meta and normalised hash, and every fact version's subject, predicate
  and value, become NULL (a deleted item keeps no content hash and no fact key);
- items.key becomes NULL and deleted_at is set; ID, kind, scope and revision count remain;
- FTS rows, embeddings, receipts and pins of the item are removed;
- an `erasures` row and one line in the sidecar ledger `erasures.jsonl` (store directory) record the item
  ID, its last revision and the time. The ledger line is written and synced before the transaction
  commits, so a crash can leave an extra ledger line (replay then erases an item again), never a missing one.

secure_delete is on for every connection, so freed cells and pages are zeroed; the storage pass in
store.maintenance (FTS optimize, VACUUM, WAL truncation) removes the remaining traces from the live file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Optional

from ..core.errors import RecallError
from ..core.normalise import canonical_json
from . import audit

LEDGER_NAME = "erasures.jsonl"


def ledger_path(store_path: Path) -> Path:
    return Path(store_path).parent / LEDGER_NAME


def append_ledger(path: Path, item_id: str, rev: int, at: int) -> None:
    """Append one content-free line to the 0600 ledger and sync it."""
    line = (canonical_json({"at": at, "item_id": item_id, "rev": rev}) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_ledger(path: Path) -> list[dict]:
    entries = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if not isinstance(entry.get("item_id"), str) or not isinstance(entry.get("rev"), int):
                    raise ValueError
            except ValueError:
                raise RecallError("invalid_input", "The erasure ledger is malformed.", line=number) from None
            entries.append(entry)
    return entries


def null_revision(db: sqlite3.Connection, item_id: str, rev: Optional[int] = None) -> int:
    """Null the content columns of one revision (or of every revision of the item). Returns rows changed."""
    where, params = ("item_id = ?", (item_id,)) if rev is None else ("item_id = ? AND rev = ?", (item_id, rev))
    changed = db.execute(
        "UPDATE item_revisions SET title = NULL, body = NULL, meta_json = NULL, body_hash = NULL"
        f" WHERE {where} AND (title IS NOT NULL OR body IS NOT NULL OR meta_json IS NOT NULL"
        " OR body_hash IS NOT NULL)", params).rowcount
    changed += db.execute(
        "UPDATE fact_versions SET subject = NULL, predicate = NULL, value_json = NULL"
        f" WHERE {where} AND (subject IS NOT NULL OR predicate IS NOT NULL OR value_json IS NOT NULL)",
        params).rowcount
    return changed


def erase_in(db: sqlite3.Connection, item_id: str, now: int, actor: str, ledger: Optional[Path]) -> dict:
    """Erase one live item inside the caller's transaction; not_found if it is absent or already deleted.
    `ledger` None skips the sidecar line (used only when replaying a ledger into a restored copy whose
    ledger already holds the entry)."""
    item = db.execute("SELECT id, project_id FROM items WHERE id = ? AND deleted_at IS NULL", (item_id,)).fetchone()
    if item is None:
        raise RecallError("not_found", "No such item.")
    db.execute("DELETE FROM fts_items WHERE rowid IN (SELECT rowid FROM item_revisions WHERE item_id = ?)",
               (item_id,))
    null_revision(db, item_id)
    db.execute("UPDATE items SET key = NULL, deleted_at = ? WHERE id = ?", (now, item_id))
    db.execute("DELETE FROM receipts WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM pins WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM embeddings WHERE item_id = ?", (item_id,))
    last_rev = db.execute("SELECT COALESCE(MAX(rev), 0) FROM item_revisions WHERE item_id = ?",
                          (item_id,)).fetchone()[0]
    db.execute("INSERT OR REPLACE INTO erasures (item_id, rev, at) VALUES (?, ?, ?)", (item_id, last_rev, now))
    audit.append(db, at=now, actor=actor, action="delete", item_id=item_id, rev=last_rev,
                 project_id=item["project_id"])
    if ledger is not None:
        append_ledger(ledger, item_id, last_rev, now)
    return {"item_id": item_id, "rev": last_rev, "at": now}
