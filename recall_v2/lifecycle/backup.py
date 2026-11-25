"""Backups, restore and artifact pruning (section 6).

Backup: the SQLite online backup API copies the live store into a new 0600 file inside a 0700 directory
(default `<store dir>/backups`); the copy's token digests are cleared (restored principals need
`principal_rotate`), it must pass `PRAGMA integrity_check` and its audit chain (copied with everything
else) must verify; then it is registered in `artifacts` with a 30-day expiry and a content-free `backup`
audit event is appended to the live store.

Documented limit: a backup holds whatever the store held when it was made. Content deleted later stays in
that backup until the backup expires and is pruned (`backup_prune`, also run by maintenance).

Restore goes only to a new path (`backup_exists` if anything is there): copy, migrate to this build's
schema, integrity check, replay the live store's erasure ledger (so items deleted after the backup are
erased again), compact, then a scan that every erased item holds no content and no index rows. Without a
ledger file, restore refuses unless no_ledger=True.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sqlite3
from typing import Optional

from ..core import model, timefmt
from ..core.errors import RecallError
from ..governance.authority import require_operator
from ..store import audit, erasure, maintenance
from ..store.connection import check_private, create_file, write_transaction
from ..store.repository import Store
from ..store.schema import APPLICATION_ID, USER_VERSION

ARTIFACT_TTL_MICROS = 30 * timefmt.MICROS_PER_DAY
BACKUP_DIR = "backups"


def private_directory(path: Path) -> Path:
    """Create a 0700 directory if missing; refuse one with the wrong owner or mode."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    check_private(path, directory=True)
    return path


def new_artifact_path(store, directory: Optional[str], path: Optional[str], subdir: str, prefix: str,
                      suffix: str, now: int) -> Path:
    """Destination of a new artifact: `path` (its directory must be private) or a fresh name in the
    default directory beside the store. The file itself is created later, exclusively, mode 0600."""
    if path is not None:
        target = Path(path).expanduser().absolute()
        check_private(target.parent, directory=True)
    else:
        folder = private_directory(Path(directory).expanduser().absolute() if directory
                                   else store.path.parent / subdir)
        stamp = timefmt.format_rfc3339(now).replace("-", "").replace(":", "").replace(".", "")
        target = folder / f"{prefix}-{stamp}-{store._ulid(now)[-6:]}{suffix}"
    if os.path.lexists(target):
        raise RecallError("backup_exists", "The destination already exists.")
    return target


def file_sha256(path: Path) -> str:
    digest = sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _remove(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _integrity(db: sqlite3.Connection) -> str:
    rows = [r[0] for r in db.execute("PRAGMA integrity_check")]
    return "ok" if rows == ["ok"] else "; ".join(rows)


def backup(session, *, path: Optional[str] = None, directory: Optional[str] = None) -> dict:
    store = require_operator(session).store
    now = store.clock()
    target = new_artifact_path(store, directory, path, BACKUP_DIR, "backup", ".sqlite3", now)
    create_file(target)
    try:
        copy = sqlite3.connect(target, isolation_level=None)
        try:
            store.db.backup(copy)
            copy.execute("PRAGMA secure_delete = ON")
            copy.execute("UPDATE principals SET token_digest = NULL")
            integrity = _integrity(copy)
            chain = audit.verify_chain(copy)
            copy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            copy.execute("PRAGMA journal_mode = DELETE")
        finally:
            copy.close()
        if integrity != "ok" or not chain["ok"]:
            raise RecallError("storage_unavailable", "The backup copy failed its integrity check.",
                              integrity=integrity, audit_ok=chain["ok"])
        os.chmod(target, 0o600)
        expires = now + ARTIFACT_TTL_MICROS
        with write_transaction(store.db) as db:
            db.execute("INSERT INTO artifacts (path, kind, created_at, expires_at) VALUES (?, 'backup', ?, ?)",
                       (str(target), now, expires))
            audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="backup")
    except BaseException:
        _remove(target)
        raise
    return {"path": str(target), "created_at": timefmt.format_rfc3339(now),
            "expires_at": timefmt.format_rfc3339(expires), "integrity": integrity, "audit": chain,
            "sha256": file_sha256(target), "tokens_cleared": True}


def prune_in(db, now: int, kinds: tuple) -> list[dict]:
    """Remove expired artifacts of the given kinds (files and rows) inside the caller's transaction."""
    pruned = []
    for row in db.execute(
            f"SELECT path, kind FROM artifacts WHERE kind IN ({','.join('?' * len(kinds))}) AND expires_at <= ?"
            " ORDER BY expires_at, path", (*kinds, now)).fetchall():
        _remove(Path(row["path"]))
        db.execute("DELETE FROM artifacts WHERE path = ?", (row["path"],))
        audit.append(db, at=now, actor=model.OPERATOR_ACTOR,
                     action="backup_prune" if row["kind"] == "backup" else "export_prune")
        pruned.append({"path": row["path"], "kind": row["kind"]})
    return pruned


def backup_prune(session) -> dict:
    """Remove backups older than the 30-day retention."""
    store = require_operator(session).store
    with write_transaction(store.db) as db:
        return {"pruned": prune_in(db, store.clock(), ("backup",))}


def _scan_erased(db: sqlite3.Connection) -> int:
    """Every erased item holds no content, key, FTS row, embedding, pin or receipt. Returns items checked."""
    bad = db.execute(
        "SELECT COUNT(*) FROM erasures AS e WHERE"
        " EXISTS (SELECT 1 FROM item_revisions AS r WHERE r.item_id = e.item_id AND (r.title IS NOT NULL"
        "   OR r.body IS NOT NULL OR r.body_hash IS NOT NULL OR r.meta_json IS NOT NULL))"
        " OR EXISTS (SELECT 1 FROM fact_versions AS f WHERE f.item_id = e.item_id AND (f.subject IS NOT NULL"
        "   OR f.predicate IS NOT NULL OR f.value_json IS NOT NULL))"
        " OR EXISTS (SELECT 1 FROM items AS i WHERE i.id = e.item_id AND (i.key IS NOT NULL OR i.deleted_at IS NULL))"
        " OR EXISTS (SELECT 1 FROM item_revisions AS r JOIN fts_items AS t ON t.rowid = r.rowid"
        "   WHERE r.item_id = e.item_id)"
        " OR EXISTS (SELECT 1 FROM embeddings AS m WHERE m.item_id = e.item_id)"
        " OR EXISTS (SELECT 1 FROM pins AS p WHERE p.item_id = e.item_id)"
        " OR EXISTS (SELECT 1 FROM receipts AS c WHERE c.item_id = e.item_id)").fetchone()[0]
    if bad:
        raise RecallError("internal", "The restored copy still holds erased content.", items=bad)
    return db.execute("SELECT COUNT(*) FROM erasures").fetchone()[0]


def restore(session, backup_path, to, *, no_ledger: bool = False) -> dict:
    store = require_operator(session).store
    if not isinstance(no_ledger, bool):
        raise RecallError("invalid_input", "no_ledger must be a boolean.")
    source = Path(backup_path).expanduser().absolute()
    target = Path(to).expanduser().absolute()
    if os.path.lexists(target):
        raise RecallError("backup_exists", "The destination already exists.")
    check_private(target.parent, directory=True)
    if not source.is_file():
        raise RecallError("not_found", "No such backup.")
    ledger = store.ledger_path
    entries: list = []
    if not no_ledger:
        if not ledger.exists():
            raise RecallError("invalid_input", "No erasure ledger beside the store; pass no_ledger=True to restore"
                                               " without replaying deletions.")
        entries = erasure.read_ledger(ledger)
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        app_id = src.execute("PRAGMA application_id").fetchone()[0]
        version = src.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID:
            raise RecallError("wrong_store", "Not a Recall backup.")
        if version > USER_VERSION:
            raise RecallError("schema_version", "The backup is newer than this build.", user_version=version)
        create_file(target)
        try:
            copy = sqlite3.connect(target, isolation_level=None)
            try:
                src.backup(copy)
            finally:
                copy.close()
        except BaseException:
            _remove(target)
            raise
    finally:
        src.close()
    try:
        os.chmod(target, 0o600)
        restored = Store.open(target, clock=store.clock)   # migrates an older schema
        try:
            integrity = _integrity(restored.db)
            if integrity != "ok":
                raise RecallError("storage_unavailable", "The restored copy failed its integrity check.",
                                  integrity=integrity)
            own_ledger = None if restored.ledger_path == ledger else restored.ledger_path
            replayed = 0
            with write_transaction(restored.db) as db:
                now = store.clock()
                for item_id in dict.fromkeys(e["item_id"] for e in entries):
                    if db.execute("SELECT 1 FROM items WHERE id = ? AND deleted_at IS NULL", (item_id,)).fetchone():
                        erasure.erase_in(db, item_id, now, model.OPERATOR_ACTOR, own_ledger)
                        replayed += 1
            maintenance.compact(restored.db)
            scanned = _scan_erased(restored.db)
            chain = audit.verify_chain(restored.db)
            integrity = _integrity(restored.db)
            restored.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            restored.close()
        with write_transaction(store.db) as db:
            audit.append(db, at=store.clock(), actor=model.OPERATOR_ACTOR, action="restore")
    except BaseException:
        _remove(target)
        raise
    return {"path": str(target), "from": str(source), "integrity": integrity, "ledger_replayed": not no_ledger,
            "replayed": replayed, "erased_items_checked": scanned, "audit": chain,
            "tokens_need_rotation": True}
