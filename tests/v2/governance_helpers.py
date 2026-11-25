"""Shared set-up for the governance tests: an operator, three principals and one project."""
from __future__ import annotations

from recall_v2.api import Client, Operator
from recall_v2.store import Store

from .helpers import StepClock, StoreTestCase

MICROS_PER_DAY = 86_400 * 1_000_000


class GovernanceCase(StoreTestCase):
    """alice and bob are members of `proj`; carol is a member of nothing."""

    embedder = None

    def setUp(self):
        self.clock = StepClock()
        self.path = self.private_dir() / "store.sqlite3"
        self.store = Store.open(self.path, create=True, clock=self.clock, embedder=self.embedder)
        self.addCleanup(self.store.close)
        self.op = Operator.open(self.store)
        self.tokens = {name: self.ok(self.op.principal_add(name))["token"] for name in ("alice", "bob", "carol")}
        self.ok(self.op.project_add("proj"))
        self.ok(self.op.member_add("alice", "proj"))
        self.ok(self.op.member_add("bob", "proj"))
        self.alice, self.bob, self.carol = (Client.open(self.store, self.tokens[n]) for n in ("alice", "bob", "carol"))

    # -- envelope assertions ---------------------------------------------------------------------

    def ok(self, envelope: dict):
        self.assertTrue(envelope["ok"], envelope)
        results = envelope["results"]
        return results[0] if len(results) == 1 else results

    def results(self, envelope: dict) -> list:
        self.assertTrue(envelope["ok"], envelope)
        return envelope["results"]

    def err(self, envelope: dict, code: str) -> dict:
        self.assertFalse(envelope["ok"], envelope)
        self.assertEqual(envelope["error"]["code"], code, envelope)
        return envelope["error"]

    def count(self, table: str, where: str = "1", params=()) -> int:
        return self.store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]

    def revision(self, item_id: str, rev: int):
        return self.store.db.execute("SELECT * FROM item_revisions WHERE item_id = ? AND rev = ?",
                                     (item_id, rev)).fetchone()

    # -- writes ----------------------------------------------------------------------------------

    def project_note(self, client, key, body, request_id=None, **kwargs):
        return self.ok(client.save(kind="note", request_id=request_id or f"r-{key}-{self.clock.now}",
                                   scope="project", project="proj", key=key, body=body, **kwargs))

    def project_fact(self, client, value, *, subject="svc", predicate="owner", request_id=None, **kwargs):
        return self.ok(client.save(kind="fact", request_id=request_id or f"f-{self.clock.now}", scope="project",
                                   project="proj", fact={"subject": subject, "predicate": predicate, "value": value},
                                   **kwargs))

    def private_fact(self, client, value, *, subject="svc", predicate="owner", request_id=None, **kwargs):
        return self.ok(client.save(kind="fact", request_id=request_id or f"p-{self.clock.now}",
                                   fact={"subject": subject, "predicate": predicate, "value": value}, **kwargs))
