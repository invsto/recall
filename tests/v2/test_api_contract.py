"""M6 contract_snapshot and no_principal_argument (spec 7.4, 7.5, 7.7).

The expected signatures below are transcribed from the spec; the test introspects the classes against
them. The error-code test reads the spec's error table (when the spec file is present) and checks that
every code is produced somewhere in this test suite.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest import mock

from recall_v2 import mcp as server
from recall_v2.api import Client, Operator, contract
from recall_v2.core.errors import ERROR_CODES
from recall_v2.store import Store

from .helpers import StoreTestCase
from .interface_helpers import setup_store

E = inspect.Parameter.empty
# Spec 7.4: name -> [(parameter, default)], all keyword-only after self.
SPEC_CLIENT = {
    "save": [("kind", E), ("request_id", E), ("scope", "private"), ("project", None), ("key", None), ("title", None),
             ("body", None), ("fact", None), ("valid_from", None), ("valid_to", None), ("expected_revision", None),
             ("source_ref", None)],
    "retract": [("item_id", E), ("rev", E), ("request_id", E), ("valid_from", None), ("valid_to", None)],
    "lookup": [("query", None), ("subject", None), ("predicate", None), ("kind", None), ("scope", None),
               ("project", None), ("as_of", None), ("known_at", None), ("limit", 10), ("explain", False)],
    "bundle": [("project", None), ("budget_tokens", 1500), ("as_of", None)],
    "get": [("item_id", E), ("rev", None), ("as_of", None), ("known_at", None)],
    "history": [("item_id", E)],
    "forget": [("item_id", E), ("request_id", E)],
    "pin": [("item_id", E)],
    "unpin": [("item_id", E)],
}
# Client methods beyond the spec list, kept deliberately: close (resource handling) and expire (M5,
# section 6 expiry by a principal in its own scope). Neither is exposed over MCP.
CLIENT_EXTRAS = {"open", "close", "expire"}
# Spec 7.5 operator commands, words joined by "_" (7.4); `principal_add(id)`, `review_approve(item_id, rev)`
# and `extract_run(source_id, ack)` have the positional shapes the spec gives.
SPEC_OPERATOR = [
    "init", "migrate", "principal_add", "principal_rotate", "principal_revoke", "principal_list", "project_add",
    "project_list", "member_add", "member_remove", "member_list", "review_list", "review_approve", "review_reject",
    "review_edit", "conflict_list", "conflict_resolve", "source_add", "source_list", "source_delete",
    "extract_plan", "extract_run", "extract_cancel", "extract_list", "delete", "expire_check", "maintain", "backup",
    "restore", "export", "audit_verify", "doctor",
]
SPEC_ERROR_CODES = {
    "invalid_input", "unauthorized", "forbidden", "not_found", "conflict", "request_id_conflict", "stale_review",
    "pin_limit", "disclosure_required", "extraction_budget_exceeded", "busy", "schema_version", "wrong_store",
    "migration_failed", "storage_unavailable", "unsafe_path", "backup_exists", "missing_dependency", "internal",
}
PRINCIPAL_WORDS = {"principal", "principal_id", "caller", "actor", "owner", "owner_id", "reviewer", "token_digest"}
SPEC_FILE = os.environ.get("RECALL_SPEC") or str(Path(__file__).resolve().parents[3] / "SPEC_RECALL_V2.md")


def public(cls) -> set[str]:
    return {name for name, value in vars(cls).items() if not name.startswith("_")
            and (inspect.isfunction(value) or isinstance(value, (classmethod, staticmethod)))}


class LibrarySignatureTests(unittest.TestCase):
    def test_client_methods_match_the_spec(self):
        self.assertEqual(public(Client) - CLIENT_EXTRAS, set(SPEC_CLIENT))
        for name, expected in SPEC_CLIENT.items():
            params = list(inspect.signature(getattr(Client, name)).parameters.values())
            self.assertEqual(params[0].name, "self", name)
            got = [(p.name, p.default) for p in params[1:]]
            self.assertEqual(got, expected, name)
            self.assertTrue(all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]), name)

    def test_client_open_takes_a_store_and_a_token(self):
        self.assertIsInstance(inspect.getattr_static(Client, "open"), classmethod)
        params = list(inspect.signature(Client.open).parameters.values())
        self.assertEqual([p.name for p in params[:2]], ["store", "token"])
        self.assertTrue(all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is None for p in params[2:]))

    def test_no_client_method_accepts_a_principal(self):
        for name in public(Client):
            params = set(inspect.signature(getattr(Client, name)).parameters)
            self.assertFalse(params & PRINCIPAL_WORDS, name)
        for name, (required, optional) in contract.OPERATIONS.items():
            self.assertFalse(set(required + optional) & PRINCIPAL_WORDS, name)

    def test_operator_has_one_method_per_operator_command(self):
        self.assertIsInstance(inspect.getattr_static(Operator, "open"), classmethod)
        params = inspect.signature(Operator.open).parameters
        self.assertEqual(list(params)[:2], ["store", "create"])
        self.assertIs(params["create"].default, False)
        self.assertEqual(params["create"].kind, inspect.Parameter.KEYWORD_ONLY)
        for name in SPEC_OPERATOR:
            self.assertTrue(callable(getattr(Operator, name, None)), name)
        self.assertEqual(list(inspect.signature(Operator.principal_add).parameters)[1:], ["principal_id"])
        self.assertEqual(list(inspect.signature(Operator.review_approve).parameters)[1:], ["item_id", "rev"])
        self.assertEqual(list(inspect.signature(Operator.extract_run).parameters)[1:3], ["source_id", "ack"])
        self.assertFalse(public(Client) & public(Operator) - {"open", "close"})

    def test_contract_table_matches_the_client(self):
        for name, (required, optional) in contract.OPERATIONS.items():
            self.assertEqual(list(required + optional), [p for p, _ in SPEC_CLIENT[name]], name)
            self.assertEqual([p for p, d in SPEC_CLIENT[name] if d is E], list(required), name)


class ErrorCodeTests(StoreTestCase):
    def test_error_list_matches_the_spec(self):
        self.assertEqual(ERROR_CODES, SPEC_ERROR_CODES)
        if not os.path.exists(SPEC_FILE):
            self.skipTest("spec file not present; checked against the transcribed list only")
        text = Path(SPEC_FILE).read_text(encoding="utf-8")
        section = text.split("### 7.7 Error codes", 1)[1].split("###", 1)[0]
        self.assertEqual(set(re.findall(r"^- `([a-z_]+)`:", section, re.MULTILINE)), SPEC_ERROR_CODES)

    def test_every_error_code_is_exercised_by_the_suite(self):
        own = Path(__file__).name
        sources = "\n".join(p.read_text(encoding="utf-8") for p in Path(__file__).parent.glob("test_*.py")
                            if p.name != own)
        sources += "\n" + "\n".join(self._produced_here())
        missing = sorted(code for code in SPEC_ERROR_CODES if f'"{code}"' not in sources)
        self.assertEqual(missing, [])

    def _produced_here(self) -> list[str]:
        """Codes no earlier milestone test produced, produced here through the interfaces."""
        path = self.private_dir() / "store.sqlite3"
        tokens = setup_store(path)
        store = Store.open(path)
        self.addCleanup(store.close)
        alice = Client(store, tokens["alice"])
        produced = []
        ids = [alice.save(kind="note", request_id=f"p{n}", key=f"k{n}", body=f"note {n}")["results"][0]["id"]
               for n in range(9)]
        for item in ids[:8]:
            self.assertTrue(alice.pin(item_id=item)["ok"])
        over = server.handle(store, "memory_pin", {"item_id": ids[8]}, tokens["alice"])
        self.assertEqual(over["error"]["code"], "pin_limit")
        produced.append('"pin_limit"')
        with mock.patch.object(Client, "history", side_effect=RuntimeError("private content")):
            failed = server.handle(store, "memory_history", {"item_id": ids[0]}, tokens["alice"])
        self.assertEqual(failed["error"], {"code": "internal", "message": "Unexpected error.", "details": {}})
        produced.append('"internal"')
        with mock.patch.dict(sys.modules, {"mcp": None}):
            with self.assertRaises(Exception) as raised:
                server.build_server(store, server.fixed_token(tokens["alice"]))
        self.assertEqual(raised.exception.code, "missing_dependency")
        produced.append('"missing_dependency"')
        return produced


class CleanInstallTests(unittest.TestCase):
    """M6 clean_install. Needs the package index (pip and setuptools), so it runs only when asked:
    RECALL_CLEAN_INSTALL=1. The script itself is tests/v2/clean_install.py."""

    @unittest.skipUnless(os.environ.get("RECALL_CLEAN_INSTALL") == "1", "set RECALL_CLEAN_INSTALL=1 (needs network)")
    def test_clean_install_under_five_minutes(self):
        script = Path(__file__).with_name("clean_install.py")
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout.splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
