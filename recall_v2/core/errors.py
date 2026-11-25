"""The fixed error-code list. Codes are the contract; messages may change."""
from __future__ import annotations

ERROR_CODES = frozenset({
    "invalid_input",
    "unauthorized",
    "forbidden",
    "not_found",
    "conflict",
    "request_id_conflict",
    "stale_review",
    "pin_limit",
    "disclosure_required",
    "extraction_budget_exceeded",
    "busy",
    "schema_version",
    "wrong_store",
    "migration_failed",
    "storage_unavailable",
    "unsafe_path",
    "backup_exists",
    "missing_dependency",
    "internal",
})


class RecallError(Exception):
    """A domain failure with a code from ERROR_CODES."""

    def __init__(self, code: str, message: str, **details):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}

    def __repr__(self) -> str:
        return f"RecallError({self.code!r}, {self.message!r})"


def invalid(message: str, **details) -> RecallError:
    return RecallError("invalid_input", message, **details)
