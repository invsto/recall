"""Exports (section 6): JSONL per project or per principal's private scope, operator only.

One canonical-JSON line per revision of every live item in the scope, ordered by item ID then rev, with
state, validity, review fields and provenance (source, adapter, URI, source status, chunk hash, run,
confidence). Deleted items (tombstones) are not exported at all. The file is created exclusively with
mode 0600 in a 0700 directory (default `<store dir>/exports`), registered in `artifacts` with a 30-day
expiry, and a content-free `export` audit event is appended.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from ..core import model, normalise, timefmt
from ..core.errors import RecallError, invalid
from ..governance.authority import require_operator
from ..store import audit
from ..store.connection import read_transaction, write_transaction
from .backup import ARTIFACT_TTL_MICROS, _remove, file_sha256, new_artifact_path

EXPORT_DIR = "exports"
FORMAT = "recall.v2.export/1"


def _ts(value):
    return None if value is None else timefmt.format_rfc3339(value)


def _lines(db, where: str, param: str):
    rows = db.execute(
        "SELECT i.id, i.kind, i.scope_kind, i.owner_id, i.project_id, i.key, i.current_rev, r.rev, r.state,"
        " r.title, r.body, r.valid_from, r.valid_to, r.actor_id, r.recorded_at, r.accepted_at, r.reviewed_by,"
        " r.reviewed_at, r.review_note, f.op, f.subject, f.predicate, f.value_json, f.supersedes_rev"
        " FROM items AS i JOIN item_revisions AS r ON r.item_id = i.id"
        " LEFT JOIN fact_versions AS f ON f.item_id = r.item_id AND f.rev = r.rev"
        f" WHERE i.deleted_at IS NULL AND {where} ORDER BY i.id, r.rev", (param,)).fetchall()
    for row in rows:
        fact = None
        if row["kind"] == "fact" and row["op"] is not None:
            fact = {"subject": row["subject"], "predicate": row["predicate"],
                    "value": None if row["value_json"] is None else json.loads(row["value_json"]),
                    "op": row["op"], "supersedes": row["supersedes_rev"]}
        provenance = [
            {"source_id": p["source_id"], "adapter": p["adapter"], "uri": p["uri"], "source_status": p["status"],
             "chunk_hash": None if p["chunk_hash"] is None else p["chunk_hash"].hex(), "run_id": p["run_id"],
             "confidence": p["confidence"]}
            for p in db.execute(
                "SELECT p.source_id, s.adapter, s.uri, s.status, p.chunk_hash, p.run_id, p.confidence"
                " FROM provenance AS p JOIN sources AS s ON s.id = p.source_id WHERE p.item_id = ? AND p.rev = ?"
                " ORDER BY p.source_id", (row["id"], row["rev"]))]
        yield {
            "format": FORMAT, "id": row["id"], "kind": row["kind"],
            "scope": {"type": row["scope_kind"],
                      "id": row["owner_id"] if row["scope_kind"] == "private" else row["project_id"]},
            "owner": row["owner_id"], "key": None if row["kind"] == "fact" else row["key"],
            "current_rev": row["current_rev"], "rev": row["rev"], "state": row["state"],
            "title": row["title"], "body": row["body"], "fact": fact,
            "valid_from": _ts(row["valid_from"]), "valid_to": _ts(row["valid_to"]),
            "actor": row["actor_id"], "recorded_at": _ts(row["recorded_at"]), "accepted_at": _ts(row["accepted_at"]),
            "reviewed_by": row["reviewed_by"], "reviewed_at": _ts(row["reviewed_at"]),
            "review_note": row["review_note"], "provenance": provenance,
        }


def export(session, *, project: Optional[str] = None, principal: Optional[str] = None, path: Optional[str] = None,
           directory: Optional[str] = None) -> dict:
    store = require_operator(session).store
    if (project is None) == (principal is None):
        raise invalid("Give exactly one of project or principal.")
    if project is not None:
        normalise.check_id(project, normalise.PROJECT_ID, "project")
        table, where, param = "projects", "i.scope_kind = 'project' AND i.project_id = ?", project
    else:
        normalise.check_id(principal, normalise.PRINCIPAL_ID, "principal")
        table, where, param = "principals", "i.scope_kind = 'private' AND i.owner_id = ?", principal
    now = store.clock()
    with read_transaction(store.db) as db:
        if db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (param,)).fetchone() is None:
            raise RecallError("not_found", f"No such {'project' if project else 'principal'}.")
        lines = list(_lines(db, where, param))
    target = new_artifact_path(store, directory, path, EXPORT_DIR, "export", ".jsonl", now)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(normalise.canonical_json(line) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        expires = now + ARTIFACT_TTL_MICROS
        with write_transaction(store.db) as db:
            db.execute("INSERT INTO artifacts (path, kind, created_at, expires_at) VALUES (?, 'export', ?, ?)",
                       (str(target), now, expires))
            audit.append(db, at=now, actor=model.OPERATOR_ACTOR, action="export", project_id=project)
    except BaseException:
        _remove(target)
        raise
    return {"path": str(target), "scope": {"type": "project" if project else "private", "id": param},
            "lines": len(lines), "items": len({line["id"] for line in lines}), "sha256": file_sha256(target),
            "created_at": timefmt.format_rfc3339(now), "expires_at": timefmt.format_rfc3339(expires)}
