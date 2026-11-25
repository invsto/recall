"""Store maintenance: compact the FTS index and rewrite the file so deleted content leaves the live store.

This is the storage part of `recall maintain`; retention and pruning arrive with the lifecycle milestone.
"""
from __future__ import annotations

import sqlite3


def compact(db: sqlite3.Connection) -> None:
    """FTS optimize, VACUUM, then a truncating WAL checkpoint. Must run outside a transaction."""
    db.execute("INSERT INTO fts_items (fts_items) VALUES ('optimize')")
    db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
