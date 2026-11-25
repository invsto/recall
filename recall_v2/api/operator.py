"""The operator library interface (section 7.4): one method per operator command, words joined by "_".

The operator is the OS user with file access to the store, not a principal; operator methods take no
token. Methods return an envelope dict and never raise for domain failures. Principal methods live on
Client; the two classes share no method names, and the governance functions behind these methods refuse
any caller that is not an OperatorSession.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Optional, Union

from .. import SCHEMA_VERSION
from ..core.errors import RecallError
from ..extraction import runs as _runs
from ..extraction import sources as _sources
from ..governance import conflicts as _conflicts
from ..governance import review as _review
from ..governance.authority import open_session
from ..lifecycle import backup as _backup
from ..lifecycle import deletion as _deletion
from ..lifecycle import doctor as _doctor
from ..lifecycle import expiry as _expiry
from ..lifecycle import export as _export
from ..lifecycle import retention as _retention
from ..retrieval.envelope import error_envelope
from ..store.repository import Store

UNSET = _review.UNSET


def _ok(op: str, results: list) -> dict:
    return {"ok": True, "schema_version": SCHEMA_VERSION, "op": op, "count": len(results), "results": results}


class Operator:
    def __init__(self, store: Store, *, owns_store: bool = False):
        self._session = open_session(store)
        self._owns_store = owns_store

    @classmethod
    def open(cls, store: Union[str, Path, Store], *, create: bool = False, clock=None, embedder=None) -> "Operator":
        if isinstance(store, Store):
            return cls(store)
        return cls(Store.open(store, create=create, clock=clock, embedder=embedder), owns_store=True)

    def close(self) -> None:
        if self._owns_store:
            self._session.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _call(self, op: str, func) -> dict:
        try:
            result = func()
        except RecallError as error:
            return error_envelope(op, error)
        except sqlite3.Error:
            return error_envelope(op, RecallError("internal", "Unexpected storage error."))
        return _ok(op, result if isinstance(result, list) else [result])

    @property
    def _store(self) -> Store:
        return self._session.store

    # -- setup -----------------------------------------------------------------------------------

    def principal_add(self, principal_id: str) -> dict:
        """The token is shown once, in this result."""
        return self._call("principal_add", lambda: {"principal_id": principal_id,
                                                    "token": self._store.principal_add(principal_id)})

    def principal_rotate(self, principal_id: str) -> dict:
        return self._call("principal_rotate", lambda: {"principal_id": principal_id,
                                                       "token": self._store.principal_rotate(principal_id)})

    def principal_revoke(self, principal_id: str) -> dict:
        def run():
            self._store.principal_revoke(principal_id)
            return {"principal_id": principal_id, "revoked": True}
        return self._call("principal_revoke", run)

    def project_add(self, project_id: str) -> dict:
        def run():
            self._store.project_add(project_id)
            return {"project_id": project_id}
        return self._call("project_add", run)

    def member_add(self, principal_id: str, project_id: str) -> dict:
        def run():
            self._store.member_add(principal_id, project_id)
            return {"principal_id": principal_id, "project_id": project_id, "member": True}
        return self._call("member_add", run)

    def member_remove(self, principal_id: str, project_id: str) -> dict:
        def run():
            self._store.member_remove(principal_id, project_id)
            return {"principal_id": principal_id, "project_id": project_id, "member": False}
        return self._call("member_remove", run)

    # -- governance ------------------------------------------------------------------------------

    def review_list(self, *, stale: bool = False) -> dict:
        return self._call("review_list", lambda: _review.list_candidates(self._session, stale=stale))

    def review_approve(self, item_id: str, rev: int) -> dict:
        return self._call("review_approve", lambda: _review.approve(self._session, item_id, rev))

    def review_reject(self, item_id: str, rev: int, *, note: Optional[str] = None) -> dict:
        return self._call("review_reject", lambda: _review.reject(self._session, item_id, rev, note=note))

    def review_edit(self, item_id: str, rev: int, *, title=UNSET, body=UNSET, value=UNSET, valid_from=UNSET,
                    valid_to=UNSET) -> dict:
        return self._call("review_edit", lambda: _review.edit(
            self._session, item_id, rev, title=title, body=body, value=value, valid_from=valid_from,
            valid_to=valid_to))

    def conflict_list(self, *, status: Optional[str] = None) -> dict:
        return self._call("conflict_list", lambda: _conflicts.list_conflicts(self._session, status=status))

    def conflict_resolve(self, conflict_id: str, action: str, *, valid_from=UNSET, valid_to=UNSET) -> dict:
        return self._call("conflict_resolve", lambda: _conflicts.resolve(
            self._session, conflict_id, action, valid_from=valid_from, valid_to=valid_to))

    # -- sources and extraction ------------------------------------------------------------------

    def source_add(self, *, adapter: str, config: dict, target: dict, **settings) -> dict:
        """Register a source; extraction starts disabled. Settings: endpoint, model_id, cloud_allowed,
        categories, redaction_rules, run_token_budget, day_token_budget, min_confidence."""
        return self._call("source_add", lambda: _sources.add(self._session, adapter=adapter, config=config,
                                                             target=target, **settings))

    def source_update(self, source_id: str, **changes) -> dict:
        return self._call("source_update", lambda: _sources.update(self._session, source_id, **changes))

    def source_list(self) -> dict:
        return self._call("source_list", lambda: _sources.list_sources(self._session))

    def extract_plan(self, source_id: str) -> dict:
        """The disclosure manifest and its SHA-256; nothing is sent anywhere."""
        return self._call("extract_plan", lambda: _runs.plan_run(self._session, source_id))

    def extract_ack(self, source_id: str, manifest_hash: str) -> dict:
        return self._call("extract_ack", lambda: _runs.acknowledge(self._session, source_id, manifest_hash))

    def extract_run(self, source_id: str, ack: str, *, model) -> dict:
        """Run under an acknowledged manifest. A run stopped at a budget returns
        extraction_budget_exceeded with the run summary in details; its candidates are kept."""
        op = "extract_run"
        try:
            summary = _runs.run(self._session, source_id, ack=ack, model=model)
        except RecallError as error:
            return error_envelope(op, error)
        except sqlite3.Error:
            return error_envelope(op, RecallError("internal", "Unexpected storage error."))
        if summary["status"] == "budget_stopped":
            return error_envelope(op, RecallError("extraction_budget_exceeded", "The run stopped at its budget.",
                                                  **summary))
        return _ok(op, [summary])

    def extract_cancel(self, run_id: str) -> dict:
        return self._call("extract_cancel", lambda: _runs.cancel(self._session, run_id))

    def extract_list(self, source_id: Optional[str] = None) -> dict:
        return self._call("extract_list", lambda: _runs.list_runs(self._session, source_id))

    # -- lifecycle -------------------------------------------------------------------------------

    def audit_verify(self) -> dict:
        return self._call("audit_verify", self._store.verify_audit)

    def expire_item(self, item_id: str, *, at=None) -> dict:
        """Policy expiry: an accepted revision that ends the item's validity at `at` (default now)."""
        return self._call("expire_item", lambda: _expiry.expire_item(self._session, item_id, at))

    def expire_check(self) -> dict:
        """Every live item expired now; lists only, deletes nothing."""
        return self._call("expire_check", lambda: _expiry.expire_check(self._session))

    def delete(self, item_id: str) -> dict:
        """Erase an item. `results` = [tombstone ITEM]; the envelope also carries `not_guaranteed` and the
        registered backups and exports (`artifacts`) that predate the deletion and still hold its content."""
        op = "delete"
        try:
            out = _deletion.delete(self._session, item_id)
        except RecallError as error:
            return error_envelope(op, error)
        except sqlite3.Error:
            return error_envelope(op, RecallError("internal", "Unexpected storage error."))
        envelope = _ok(op, [out["tombstone"]])
        envelope["not_guaranteed"] = out["not_guaranteed"]
        envelope["artifacts"] = out["artifacts"]
        return envelope

    def source_delete(self, source_id: str, *, cascade: bool = False) -> dict:
        return self._call("source_delete", lambda: _deletion.source_delete(self._session, source_id,
                                                                           cascade=cascade))

    def maintain(self) -> dict:
        """Retention, artifact pruning, FTS optimize and VACUUM. Idempotent."""
        return self._call("maintain", lambda: _retention.maintenance(self._session))

    def backup(self, *, path: Optional[str] = None, directory: Optional[str] = None, prune: bool = False) -> dict:
        def run():
            result = _backup.backup(self._session, path=path, directory=directory)
            if prune:
                result["pruned"] = _backup.backup_prune(self._session)["pruned"]
            return result
        return self._call("backup", run)

    def backup_prune(self) -> dict:
        return self._call("backup_prune", lambda: _backup.backup_prune(self._session))

    def restore(self, backup: str, *, to: str, no_ledger: bool = False) -> dict:
        return self._call("restore", lambda: _backup.restore(self._session, backup, to, no_ledger=no_ledger))

    def export(self, *, project: Optional[str] = None, principal: Optional[str] = None, path: Optional[str] = None,
               directory: Optional[str] = None) -> dict:
        return self._call("export", lambda: _export.export(self._session, project=project, principal=principal,
                                                           path=path, directory=directory))

    def doctor(self) -> dict:
        """Read-only health report (a JSON-serialisable dict with a fixed key order)."""
        return self._call("doctor", lambda: _doctor.doctor(self._session))
