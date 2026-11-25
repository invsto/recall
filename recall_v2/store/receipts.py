"""Idempotency receipts keyed by (principal, request_id) with a SHA-256 request fingerprint."""
from __future__ import annotations

from hashlib import sha256
import sqlite3
from typing import Optional

from ..core.errors import RecallError
from ..core.model import RECEIPT_TTL_MICROS
from ..core.normalise import canonical_json

SCHEME = 2


def fingerprint(payload: dict) -> bytes:
    return sha256(canonical_json(payload).encode("utf-8")).digest()


def replay(db: sqlite3.Connection, principal_id: str, request_id: str, digest: bytes, now: int,
           *, scheme: int = SCHEME) -> Optional[dict]:
    """Return the original result for a matching replay, None for a new request.

    A different fingerprint (or a receipt of another scheme) is request_id_conflict. A receipt older
    than 30 days is dropped and the request is treated as new.
    """
    row = db.execute(
        "SELECT fingerprint, scheme, item_id, rev, recorded_at FROM receipts WHERE principal_id = ? AND request_id = ?",
        (principal_id, request_id),
    ).fetchone()
    if row is None:
        return None
    if row["recorded_at"] < now - RECEIPT_TTL_MICROS:
        db.execute("DELETE FROM receipts WHERE principal_id = ? AND request_id = ?", (principal_id, request_id))
        return None
    if row["scheme"] != scheme or row["fingerprint"] != digest:
        raise RecallError("request_id_conflict", "The request_id is bound to a different request.")
    return {"item_id": row["item_id"], "rev": row["rev"]}


def record(db: sqlite3.Connection, principal_id: str, request_id: str, digest: bytes, item_id: str, rev: int,
           now: int, *, scheme: int = SCHEME) -> None:
    db.execute(
        "INSERT INTO receipts (principal_id, request_id, fingerprint, scheme, item_id, rev, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (principal_id, request_id, digest, scheme, item_id, rev, now),
    )
