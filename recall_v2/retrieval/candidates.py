"""Candidate-set SQL shared by every retrieval leg and the bundle.

Every query here starts from the single visibility predicate (store.visibility.visible_items) and the
current-truth rule of section 2.5 at (:tv, :tk). Scope and time are predicates of the candidate query,
never scores: invisible or non-current revisions are never read, ranked or counted.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from ..core.timefmt import format_rfc3339
from ..store.repository import CURRENT_FACT_REV, CURRENT_OTHER_REV, REVISION_COLUMNS, REVISION_FROM, revision_dict
from ..store.visibility import visible_items

CURRENT_REV = f"(CASE WHEN i.kind = 'fact' THEN {CURRENT_FACT_REV} ELSE {CURRENT_OTHER_REV} END)"

# Visible item, its current accepted revision at (:tv, :tk); facts only when the winner is an assert.
CURRENT = (
    f"{visible_items('i')} AND r.state = 'accepted' AND r.rev = {CURRENT_REV}"
    " AND (i.kind <> 'fact' OR f.op = 'assert')"
    " AND r.valid_from <= :tv AND (r.valid_to IS NULL OR :tv < r.valid_to)"
)

# Optional lookup filters (NULL = no filter).
FILTERS = (
    " AND (:kind IS NULL OR i.kind = :kind) AND (:scope IS NULL OR i.scope_kind = :scope)"
    " AND (:project IS NULL OR i.project_id = :project)"
)

COLUMNS = f"{REVISION_COLUMNS}, r.rowid AS rid"


def is_member(db: sqlite3.Connection, caller: str, project: str) -> bool:
    return db.execute("SELECT 1 FROM memberships WHERE principal_id = ? AND project_id = ?",
                      (caller, project)).fetchone() is not None


def enrich(db: sqlite3.Connection, caller: str, row: sqlite3.Row) -> dict:
    """The full ITEM of section 4.4 for one visible revision row. Flags carry only their type, so no
    flag can reveal another (possibly invisible) item's ID."""
    item = revision_dict(row)
    item["sources"] = [
        {"source_id": s["id"], "adapter": s["adapter"], "uri": s["uri"],
         "captured_at": item_ts(s["captured_at"]), "deleted": s["status"] == "deleted"}
        for s in db.execute(
            "SELECT s.id, s.adapter, s.uri, s.captured_at, s.status FROM provenance AS p"
            " JOIN sources AS s ON s.id = p.source_id WHERE p.item_id = ? AND p.rev = ? ORDER BY s.id",
            (row["item_id"], row["rev"]))]
    item["flags"] = [f[0] for f in db.execute(
        "SELECT DISTINCT type FROM flags WHERE item_id = ? AND rev = ? ORDER BY type", (row["item_id"], row["rev"]))]
    item["pinned"] = db.execute("SELECT 1 FROM pins WHERE principal_id = ? AND item_id = ?",
                                (caller, row["item_id"])).fetchone() is not None
    item["rank"] = None
    item["score"] = None
    item["trust"] = "untrusted"
    return item


def item_ts(value: Optional[int]) -> Optional[str]:
    return None if value is None else format_rfc3339(value)


__all__ = ["CURRENT", "CURRENT_REV", "FILTERS", "COLUMNS", "REVISION_FROM", "enrich", "is_member"]
