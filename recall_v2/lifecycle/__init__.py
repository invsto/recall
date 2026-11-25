"""Lifecycle (section 6): expiry, deletion and source deletion, retention and maintenance, backups and
restore, exports, and the read-only doctor report.

Every function except the principal paths (`expiry.expire_as`, `deletion.forget`) is an operator
function: its first argument must be an OperatorSession, anything else is refused with `forbidden`.
Modules are imported directly (``from recall_v2.lifecycle import backup``).
"""
