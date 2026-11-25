"""The operator capability. Operator functions accept only an OperatorSession; a principal handle,
a client or anything else is refused with `forbidden` before any read or write."""
from __future__ import annotations

from ..core.errors import RecallError

_SEAL = object()


class OperatorSession:
    """Proof that the caller opened the store as the operator (section 5: the operator is the OS user
    with file access, not a principal). Created only by recall_v2.api.Operator."""

    __slots__ = ("store",)

    def __init__(self, store, seal):
        if seal is not _SEAL:
            raise RecallError("forbidden", "Only the operator may do this.")
        self.store = store


def open_session(store) -> OperatorSession:
    return OperatorSession(store, _SEAL)


def require_operator(session) -> "OperatorSession":
    if type(session) is not OperatorSession:
        raise RecallError("forbidden", "Only the operator may do this.")
    return session
