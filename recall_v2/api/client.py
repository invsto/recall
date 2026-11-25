"""The principal library interface (section 7.4). Every method returns the section 4.4 envelope as a dict
and never raises for domain failures. No method accepts a principal: identity comes only from the token.

Write path (this milestone): `save` and `retract` run the store's single IMMEDIATE write transaction
(receipt replay, compare-and-swap on expected_revision, exact dedupe, conflict detection, near-duplicate
flags for candidates, audit), then return the saved ITEM as the caller sees it.

Receipts. A request is keyed by (token's principal, request_id) with a SHA-256 fingerprint of the
canonical request. A matching replay returns the original revision and writes nothing; the same
request_id with different content is `request_id_conflict`. Receipts are kept for 30 days; after that a
replay is treated as a new request. Pass expected_revision when a late replay must not write again:
compare-and-swap then rejects it with `conflict`.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sqlite3
from typing import Optional, Union

from .. import SCHEMA_VERSION
from ..core import timefmt
from ..core.errors import RecallError
from ..lifecycle import deletion as _deletion
from ..lifecycle import expiry as _expiry
from ..retrieval import bundle as _bundle
from ..retrieval import lookup as _lookup
from ..retrieval.candidates import COLUMNS, enrich
from ..retrieval.envelope import NOTICE, error_envelope
from ..store.connection import read_transaction
from ..store.repository import REVISION_FROM, Principal, Store
from ..store.visibility import visible_items, visible_revisions


def _principal(token) -> Principal:
    """A handle carrying only the token digest; each call re-checks it, so revocation applies at once."""
    raw = token.encode("utf-8", "surrogatepass") if isinstance(token, str) else repr(token).encode("utf-8")
    return Principal("", sha256(raw).digest())


def _visible_item(db, caller: str, item_id: str, rev: int) -> Optional[dict]:
    row = db.execute(
        f"SELECT {COLUMNS} FROM {REVISION_FROM} WHERE i.id = :id AND r.rev = :rev AND {visible_items('i')}"
        f" AND {visible_revisions('r', 'i')}", {"id": item_id, "rev": rev, "caller": caller}).fetchone()
    return None if row is None else enrich(db, caller, row)


class Client:
    """A principal's view of one store."""

    def __init__(self, store: Store, token: str, *, owns_store: bool = False):
        self._store = store
        self._principal = _principal(token)
        self._owns_store = owns_store

    @classmethod
    def open(cls, store: Union[str, Path, Store], token: str, *, clock=None, embedder=None) -> "Client":
        """Open a store path (or wrap an already open Store) with a bearer token."""
        if isinstance(store, Store):
            return cls(store, token)
        return cls(Store.open(store, clock=clock, embedder=embedder), token, owns_store=True)

    def close(self) -> None:
        if self._owns_store:
            self._store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- helpers ---------------------------------------------------------------------------------

    def _items_envelope(self, op: str, results: list[dict], at: int) -> dict:
        stamp = timefmt.format_rfc3339(at)
        return {"ok": True, "schema_version": SCHEMA_VERSION, "op": op, "as_of": stamp, "known_at": stamp,
                "notice": NOTICE, "count": len(results), "truncated": False, "results": results}

    def _call(self, op: str, func) -> dict:
        try:
            return func()
        except RecallError as error:
            return error_envelope(op, error)
        except sqlite3.Error:
            return error_envelope(op, RecallError("internal", "Unexpected storage error."))

    def _saved(self, op: str, result: dict) -> dict:
        """The saved revision as the caller sees it. as_of and known_at are the revision's recorded_at, so
        a replay returns an identical envelope while the revision is unchanged."""
        with read_transaction(self._store.db) as db:
            caller = self._store._caller(db, self._principal.token_digest)
            item = _visible_item(db, caller, result["item_id"], result["rev"])
            if item is None:
                raise RecallError("not_found", "No such item.")
            recorded = db.execute("SELECT recorded_at FROM item_revisions WHERE item_id = ? AND rev = ?",
                                  (result["item_id"], result["rev"])).fetchone()[0]
        return self._items_envelope(op, [item], recorded)

    # -- writes ----------------------------------------------------------------------------------

    def save(self, *, kind, request_id, scope="private", project=None, key=None, title=None, body=None, fact=None,
             valid_from=None, valid_to=None, expected_revision=None, source_ref=None) -> dict:
        """Write one revision: private saves are accepted, project saves are candidates for review.
        `results` = [saved ITEM]."""
        def run():
            result = self._store.save(self._principal, kind=kind, request_id=request_id, scope=scope,
                                      project=project, key=key, title=title, body=body, fact=fact,
                                      valid_from=valid_from, valid_to=valid_to,
                                      expected_revision=expected_revision, source_ref=source_ref)
            return self._saved("save", result)
        return self._call("save", run)

    def retract(self, *, item_id, rev, request_id, valid_from=None, valid_to=None) -> dict:
        """Retract a fact revision (the value was wrong). `results` = [saved ITEM]."""
        def run():
            result = self._store.retract(self._principal, item_id=item_id, rev=rev, request_id=request_id,
                                         valid_from=valid_from, valid_to=valid_to)
            return self._saved("retract", result)
        return self._call("retract", run)

    def expire(self, *, item_id, request_id, at=None) -> dict:
        """End a visible item's validity at `at` (default now) with a new revision: accepted in the
        caller's private scope, a candidate for review in a project. `results` = [saved ITEM]."""
        def run():
            result = _expiry.expire_as(self._store, self._principal, item_id=item_id, request_id=request_id, at=at)
            return self._saved("expire", result)
        return self._call("expire", run)

    def forget(self, *, item_id, request_id) -> dict:
        """Delete an item the caller owns (private scope) or a project item made only of candidates it
        authored. `results` = [tombstone ITEM]; `not_guaranteed` lists what deletion cannot promise."""
        def run():
            out = _deletion.forget(self._store, self._principal, item_id=item_id, request_id=request_id)
            envelope = self._items_envelope("forget", [out["tombstone"]], self._store.clock())
            envelope["not_guaranteed"] = out["not_guaranteed"]
            envelope["artifacts_predating"] = out["artifacts_predating"]
            return envelope
        return self._call("forget", run)

    # -- reads -----------------------------------------------------------------------------------

    def lookup(self, *, query=None, subject=None, predicate=None, kind=None, scope=None, project=None, as_of=None,
               known_at=None, limit=10, explain=False) -> dict:
        return self._call("lookup", lambda: _lookup(
            self._store, self._principal, query=query, subject=subject, predicate=predicate, kind=kind,
            scope=scope, project=project, as_of=as_of, known_at=known_at, limit=limit, explain=explain))

    def bundle(self, *, project=None, budget_tokens=1500, as_of=None) -> dict:
        return self._call("bundle", lambda: _bundle(self._store, self._principal, project=project,
                                                    budget_tokens=budget_tokens, as_of=as_of))

    def get(self, *, item_id, rev=None, as_of=None, known_at=None) -> dict:
        def run():
            found = self._store.get(self._principal, item_id=item_id, rev=rev, as_of=as_of, known_at=known_at)
            with read_transaction(self._store.db) as db:
                caller = self._store._caller(db, self._principal.token_digest)
                item = _visible_item(db, caller, found["id"], found["rev"])
            if item is None:
                raise RecallError("not_found", "No such item.")
            return self._items_envelope("get", [item], self._store.clock())
        return self._call("get", run)

    def history(self, *, item_id) -> dict:
        def run():
            entries = self._store.history(self._principal, item_id=item_id)
            out = []
            with read_transaction(self._store.db) as db:
                caller = self._store._caller(db, self._principal.token_digest)
                for entry in entries:
                    item = _visible_item(db, caller, entry["id"], entry["rev"])
                    if item is not None:
                        item["current"] = entry["current"]
                        out.append(item)
            return self._items_envelope("history", out, self._store.clock())
        return self._call("history", run)

    def _pin_result(self, op: str, item_id: str) -> dict:
        with read_transaction(self._store.db) as db:
            caller = self._store._caller(db, self._principal.token_digest)
            row = db.execute("SELECT current_rev FROM items WHERE id = ?", (item_id,)).fetchone()
            item = _visible_item(db, caller, item_id, row["current_rev"]) if row and row["current_rev"] else None
        results = [item] if item is not None else []
        return self._items_envelope(op, results, self._store.clock())

    def pin(self, *, item_id) -> dict:
        def run():
            self._store.pin(self._principal, item_id=item_id)
            return self._pin_result("pin", item_id)
        return self._call("pin", run)

    def unpin(self, *, item_id) -> dict:
        def run():
            self._store.unpin(self._principal, item_id=item_id)
            return self._pin_result("unpin", item_id)
        return self._call("unpin", run)


__all__ = ["Client"]
