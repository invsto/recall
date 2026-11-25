"""Near-duplicate flags (section 3.3). Flags inform review; flagged items are never merged.

A candidate gets a `dedupe` flag when another open candidate exists for the same fact key (a fact key
is one item, so: another candidate revision of the same fact item), or, when an embedder is supplied,
when its cosine similarity to an accepted revision of another item that is visible to the author and in
the same scope is at least NEAR_DUPLICATE_SIMILARITY. Comparison never crosses scope.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from ..store import embeddings
from ..store.visibility import visible_items

NEAR_DUPLICATE_SIMILARITY = 0.92


def flag_near_duplicates(db: sqlite3.Connection, embedder, *, caller: str, item_id: str, rev: int, kind: str,
                         scope_kind: str, owner_id: str, project_id: Optional[str], text: str, now: int) -> list[str]:
    """Insert dedupe flags for candidate (item_id, rev); return the flagged other item IDs."""
    others: set[str] = set()
    if kind == "fact" and db.execute(
            "SELECT 1 FROM item_revisions WHERE item_id = ? AND state = 'candidate' AND rev <> ? LIMIT 1",
            (item_id, rev)).fetchone():
        others.add(item_id)
    if embedder is not None:
        [vector] = embedder.embed([text])
        rows = db.execute(
            "SELECT e.item_id, e.vector FROM embeddings AS e JOIN items AS i ON i.id = e.item_id"
            " JOIN item_revisions AS r ON r.item_id = e.item_id AND r.rev = e.rev"
            f" WHERE e.model = :model AND r.state = 'accepted' AND i.id <> :item AND {visible_items('i')}"
            " AND i.scope_kind = :scope AND ((i.scope_kind = 'private' AND i.owner_id = :owner)"
            " OR (i.scope_kind = 'project' AND i.project_id = :project))",
            {"model": embedder.model, "item": item_id, "caller": caller, "scope": scope_kind, "owner": owner_id,
             "project": project_id}).fetchall()
        for row in rows:
            if embeddings.cosine(vector, embeddings.decode(row["vector"])) >= NEAR_DUPLICATE_SIMILARITY:
                others.add(row["item_id"])
    for other in sorted(others):
        db.execute("INSERT OR IGNORE INTO flags (item_id, rev, type, other_item_id, created_at)"
                   " VALUES (?, ?, 'dedupe', ?, ?)", (item_id, rev, other, now))
    return sorted(others)
