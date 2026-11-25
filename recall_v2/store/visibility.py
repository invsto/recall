"""The single visibility predicate. Every read path builds its candidate set from these fragments.

Both fragments use the named parameter :caller (an authenticated principal ID).
"""
from __future__ import annotations


def visible_items(item: str = "i") -> str:
    """visible(caller): not deleted, and private with owner = caller, or in a project where caller is a member."""
    return (
        f"({item}.deleted_at IS NULL AND ("
        f"({item}.scope_kind = 'private' AND {item}.owner_id = :caller)"
        f" OR ({item}.scope_kind = 'project' AND EXISTS ("
        f"SELECT 1 FROM memberships AS vis_m"
        f" WHERE vis_m.principal_id = :caller AND vis_m.project_id = {item}.project_id))))"
    )


def visible_revisions(revision: str = "r", item: str = "i") -> str:
    """Within a visible item: accepted revisions, plus candidates or rejections the caller authored
    or that sit in the caller's private scope."""
    return (
        f"({revision}.state = 'accepted' OR {revision}.actor_id = :caller"
        f" OR ({item}.scope_kind = 'private' AND {item}.owner_id = :caller))"
    )
