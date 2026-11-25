"""SQLite connections: version checks, pragmas, file safety and transactions."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import time

from ..core.errors import RecallError
from .schema import APPLICATION_ID, FIRST_VERSION, MIGRATIONS, USER_VERSION

MIN_SQLITE = (3, 37, 0)
BUSY_TIMEOUT_MS = 5000
BUSY_RETRY_DELAYS = (0.05, 0.1, 0.2)


def check_engine() -> None:
    """Refuse SQLite older than 3.37 (STRICT tables) or a build without FTS5."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RecallError("storage_unavailable", "SQLite 3.37 or newer is required.",
                          sqlite_version=sqlite3.sqlite_version)
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
    except sqlite3.Error:
        raise RecallError("storage_unavailable", "SQLite FTS5 is required.") from None
    finally:
        probe.close()


def check_private(path: Path, *, directory: bool = False) -> None:
    """Store directory 0700 and store file 0600, owned by the operator, not hard-linked."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise RecallError("wrong_store", "Not a Recall store.") from None
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not kind_ok or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise RecallError("unsafe_path", "Use an operator-owned 0700 directory and a 0600 store file.")


def _configure(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA secure_delete = ON")
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA temp_store = MEMORY")
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RecallError("storage_unavailable", "SQLite foreign keys are unavailable.")
    if db.execute("PRAGMA secure_delete").fetchone()[0] != 1:
        raise RecallError("storage_unavailable", "SQLite secure_delete is unavailable.")


def raw_connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None,
                         timeout=BUSY_TIMEOUT_MS / 1000)
    db.row_factory = sqlite3.Row
    try:
        _configure(db)
    except BaseException:
        db.close()
        raise
    return db


def create_file(path: Path) -> None:
    """Create an empty 0600 store file; refuse an existing path."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RecallError("backup_exists", "The destination already exists.") from None
    os.close(fd)


def initialise(db: sqlite3.Connection) -> None:
    """Bring an empty or current store to USER_VERSION; refuse foreign or newer stores with zero writes."""
    app_id = db.execute("PRAGMA application_id").fetchone()[0]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    empty = db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is None
    if app_id == 0 and version == 0 and empty:
        script = "\n".join(MIGRATIONS[step] for step in range(FIRST_VERSION, USER_VERSION + 1))
        try:
            db.executescript(
                "BEGIN IMMEDIATE;\n" + script
                + f"\nPRAGMA application_id = {APPLICATION_ID};\nPRAGMA user_version = {USER_VERSION};\n"
                + "INSERT INTO meta (name, value) VALUES ('estimator', 'bytes3-v1');\nCOMMIT;"
            )
        except sqlite3.Error:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        db.execute("PRAGMA journal_mode = WAL")
        return
    if app_id == APPLICATION_ID and FIRST_VERSION <= version < USER_VERSION:
        upgrade(db, version)
        return
    check_version(app_id, version)


def upgrade(db: sqlite3.Connection, version: int) -> None:
    """Apply every step after `version` in one IMMEDIATE transaction; on failure nothing changes."""
    script = "\n".join(MIGRATIONS[step] for step in range(version + 1, USER_VERSION + 1))
    try:
        db.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {USER_VERSION};\nCOMMIT;")
    except sqlite3.Error as exc:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise RecallError("migration_failed", "The schema upgrade was rolled back.",
                          reason=type(exc).__name__) from None


def check_version(app_id: int, version: int) -> None:
    if app_id != APPLICATION_ID:
        raise RecallError("wrong_store", "Not a Recall store.")
    if version > USER_VERSION:
        raise RecallError("schema_version", "The store is newer than this build.", user_version=version)
    if version != USER_VERSION:
        raise RecallError("wrong_store", "Not a Recall v2 store; use migrate for a v1 store.",
                          user_version=version)


@contextmanager
def write_transaction(db: sqlite3.Connection):
    """One BEGIN IMMEDIATE per write; SQLITE_BUSY retried 3 times (50, 100, 200 ms), then busy."""
    for attempt in range(len(BUSY_RETRY_DELAYS) + 1):
        try:
            db.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if attempt == len(BUSY_RETRY_DELAYS):
                raise RecallError("busy", "The store is locked.") from None
            time.sleep(BUSY_RETRY_DELAYS[attempt])
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


@contextmanager
def read_transaction(db: sqlite3.Connection):
    db.execute("BEGIN")
    try:
        yield db
    finally:
        db.execute("ROLLBACK")
