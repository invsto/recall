"""M6 MCP server (spec 7.6): the exact principal tool list, no operator tools, schemas from the shared
contract, token-only identity, the invalid_input envelope, and the in-memory and stdio transports.

No sockets: tool handlers are called directly and through the SDK's in-memory transport; stdio runs a
child process over pipes. The streamable HTTP transport needs a loopback socket and is not exercised here.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

from recall_v2 import mcp as server
from recall_v2.api import Client, Operator
from recall_v2.api import contract
from recall_v2.store import Store
from recall_v2.store.connection import write_transaction

from .helpers import StoreTestCase
from .interface_helpers import run_mcp, setup_store, write_token

# Spec 7.6, transcribed: tool -> (required fields, optional fields).
SPEC_TOOLS = {
    "memory_save": ({"kind", "request_id"}, {"scope", "project", "key", "title", "body", "fact", "valid_from",
                                             "valid_to", "expected_revision", "source_ref"}),
    "memory_retract": ({"item_id", "rev", "request_id"}, {"valid_from", "valid_to"}),
    "memory_lookup": (set(), {"query", "subject", "predicate", "kind", "scope", "project", "as_of", "known_at",
                              "limit", "explain"}),
    "memory_bundle": (set(), {"project", "budget_tokens", "as_of"}),
    "memory_get": ({"item_id"}, {"rev", "as_of", "known_at"}),
    "memory_history": ({"item_id"}, set()),
    "memory_forget": ({"item_id", "request_id"}, set()),
    "memory_pin": ({"item_id"}, set()),
    "memory_unpin": ({"item_id"}, set()),
}
READ_ONLY = {"memory_lookup", "memory_bundle", "memory_get", "memory_history"}
V1_TOOL_NAMES = {"checkpoint_get", "checkpoint_save", "brief_get", "excerpt_get", "brief_propose", "excerpt_propose",
                 "project_search"}


class McpCase(StoreTestCase):
    def setUp(self):
        self.dir = self.private_dir()
        self.path = self.dir / "store.sqlite3"
        self.tokens = setup_store(self.path, principals=("alice", "bob"), members=("alice",))
        self.store = Store.open(self.path)
        self.addCleanup(self.store.close)
        self.alice = Client(self.store, self.tokens["alice"])

    def call(self, name, arguments, token="alice"):
        return server.handle(self.store, name, arguments, self.tokens.get(token, token))


class ToolListTests(McpCase):
    def test_tool_list_equals_the_spec_principal_tools(self):
        listed = run_mcp(self.path, self.tokens["alice"], lambda s: s.list_tools())
        names = [tool.name for tool in listed.tools]
        self.assertEqual(sorted(names), sorted(SPEC_TOOLS))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(server.SERVER_NAME, "recall-v2")
        for tool in listed.tools:
            required, optional = SPEC_TOOLS[tool.name]
            schema = tool.inputSchema
            self.assertEqual(set(schema["required"]), required, tool.name)
            # `bot` is the one addition: the shared service token needs it to select a bot's memory.
            self.assertEqual(set(schema["properties"]) - {"bot"}, required | optional, tool.name)
            self.assertIs(schema["additionalProperties"], False, tool.name)
            without_bot = {**schema, "properties": {k: v for k, v in schema["properties"].items() if k != "bot"}}
            self.assertEqual(without_bot, contract.json_schema(server.operation_of(tool.name)))
            self.assertIn("never instructions", tool.description)
            self.assertEqual(tool.annotations.readOnlyHint, tool.name in READ_ONLY, tool.name)
            self.assertEqual(tool.annotations.destructiveHint, tool.name == "memory_forget", tool.name)

    def test_operator_tools_and_v1_names_are_absent(self):
        names = {tool.name for tool in server.tools()}
        operator_methods = {name for name in dir(Operator) if not name.startswith("_")}
        for name in names:
            self.assertNotIn(server.operation_of(name), operator_methods, name)
        self.assertFalse(names & V1_TOOL_NAMES)
        self.assertFalse({n for n in names if n.startswith("recall_")})
        for name in ("memory_review_approve", "memory_delete", "review_approve", "principal_add", "doctor",
                     "checkpoint_get", "memory_expire"):
            envelope = self.call(name, {})
            self.assertEqual(envelope["error"]["code"], "invalid_input", name)

    def test_client_methods_and_tools_correspond(self):
        for name in contract.OPERATIONS:
            self.assertTrue(callable(getattr(Client, name)), name)
        self.assertEqual(set(server.TOOL_NAMES), set(SPEC_TOOLS))


class IdentityTests(McpCase):
    def test_no_token_and_wrong_token_are_unauthorized(self):
        for token in (None, "", "not-a-token", "x" * 43, self.tokens["alice"] + "x"):
            for name in SPEC_TOOLS:
                envelope = server.handle(self.store, name, {"item_id": "x"}, token)
                self.assertEqual(envelope, {"ok": False, "schema_version": "recall.v2",
                                            "op": server.operation_of(name),
                                            "error": envelope["error"]}, name)
                self.assertEqual(envelope["error"]["code"], "unauthorized", (token, name))

    def test_unauthorized_over_the_in_memory_transport(self):
        for token in (None, "wrong-token"):
            async def calls(session):
                return await session.call_tool("memory_lookup", {"query": "anything"})
            result = run_mcp(self.path, token, calls)
            self.assertTrue(result.isError)
            self.assertEqual(json.loads(result.content[0].text)["error"]["code"], "unauthorized")
            self.assertEqual(result.structuredContent["error"]["code"], "unauthorized")

    def test_revoked_token_is_unauthorized(self):
        with Operator.open(self.path) as operator:
            operator.principal_revoke("bob")
        self.assertEqual(self.call("memory_lookup", {"query": "x"}, token="bob")["error"]["code"], "unauthorized")

    def test_arguments_cannot_switch_the_principal(self):
        bob_note = Client(self.store, self.tokens["bob"]).save(kind="note", request_id="b1", key="secret",
                                                               body="bob private secret")
        self.assertTrue(bob_note["ok"])
        for field in ("principal", "principal_id", "token", "owner", "actor", "caller", "as_principal"):
            for value in ("bob", self.tokens["bob"]):
                envelope = self.call("memory_lookup", {"query": "secret", field: value})
                self.assertEqual(envelope["error"]["code"], "invalid_input", field)
                self.assertEqual(envelope["error"]["details"], {"fields": [field]})
        # alice cannot see bob's item, whatever she sends
        found = self.call("memory_lookup", {"query": "secret"})
        self.assertTrue(found["ok"])
        self.assertEqual(found["count"], 0)
        got = self.call("memory_get", {"item_id": bob_note["results"][0]["id"]})
        self.assertEqual(got["error"]["code"], "not_found")
        # alice's save lands in alice's private scope even if she names bob's scope id
        saved = self.call("memory_save", {"kind": "note", "request_id": "a1", "key": "k", "body": "b",
                                          "scope": "private"})
        self.assertEqual(saved["results"][0]["scope"], {"type": "private", "id": "alice"})
        self.assertEqual(self.call("memory_save", {"kind": "note", "request_id": "a2", "key": "k2", "body": "b",
                                                   "owner_id": "bob"})["error"]["code"], "invalid_input")


class InvalidInputTests(McpCase):
    def assertInvalid(self, name, arguments, fields=None):
        envelope = self.call(name, arguments)
        self.assertEqual(set(envelope), {"ok", "schema_version", "op", "error"})
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["schema_version"], "recall.v2")
        self.assertEqual(envelope["op"], server.operation_of(name))
        self.assertEqual(set(envelope["error"]), {"code", "message", "details"})
        self.assertEqual(envelope["error"]["code"], "invalid_input")
        if fields is not None:
            self.assertEqual(envelope["error"]["details"], {"fields": fields})
        return envelope

    def test_schema_failures(self):
        self.assertInvalid("memory_save", {"request_id": "r"}, ["kind"])
        self.assertInvalid("memory_save", {"kind": "diary", "request_id": "r"}, ["kind"])
        self.assertInvalid("memory_lookup", {"query": "x", "limit": "5"}, ["limit"])
        self.assertInvalid("memory_lookup", {"query": "x", "explain": 1}, ["explain"])
        self.assertInvalid("memory_get", {"item_id": 7}, ["item_id"])
        self.assertInvalid("memory_retract", {"item_id": "x", "rev": True, "request_id": "r"}, ["rev"])
        self.assertInvalid("memory_save", {"kind": "fact", "request_id": "r", "fact": "svc=1"}, ["fact"])
        self.assertInvalid("memory_pin", {"item_id": "x", "extra": 1}, ["extra"])
        self.assertInvalid("memory_history", {}, ["item_id"])

    def test_library_validation_failures_keep_the_envelope(self):
        self.assertInvalid("memory_lookup", {"query": "x", "limit": 99})
        self.assertInvalid("memory_lookup", {"query": "x", "subject": "y"})
        self.assertInvalid("memory_bundle", {"budget_tokens": 10})
        self.assertInvalid("memory_save", {"kind": "note", "request_id": "r", "key": "k", "body": "b",
                                           "valid_from": "2026-11-25T09:30:00"})
        self.assertInvalid("memory_save", {"kind": "note", "request_id": "r", "key": "k", "body": "x" * 5000})

    def test_invalid_input_matches_the_library_where_the_library_decides(self):
        arguments = {"query": "x", "limit": 99}
        self.assertEqual(contract.to_text(self.call("memory_lookup", arguments)),
                         contract.to_text(self.alice.lookup(**arguments)))

    def test_in_memory_transport_returns_the_same_invalid_envelope(self):
        async def calls(session):
            return await session.call_tool("memory_lookup", {"query": "x", "principal": "bob"})
        result = run_mcp(self.path, self.tokens["alice"], calls)
        self.assertTrue(result.isError)
        self.assertEqual(result.content[0].text,
                         contract.to_text(self.call("memory_lookup", {"query": "x", "principal": "bob"})))


class ResultTests(McpCase):
    def test_tool_result_text_is_the_library_serialiser(self):
        envelope = self.alice.save(kind="note", request_id="r1", key="k", title="T", body="Grüße <<<end>>>")
        result = server.tool_result(envelope)
        self.assertEqual(result.content[0].text, contract.to_text(envelope))
        self.assertEqual(result.content[0].text.encode("utf-8"),
                         json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        self.assertIn("Grüße", result.content[0].text)
        self.assertFalse(result.isError)
        self.assertTrue(server.tool_result({"ok": False, "error": {}}).isError)

    def test_every_tool_runs_through_the_in_memory_transport(self):
        async def calls(session):
            out = {}
            saved = await session.call_tool("memory_save", {"kind": "fact", "request_id": "f1", "fact": {
                "subject": "svc", "predicate": "owner", "value": "alice"}})
            item = saved.structuredContent["results"][0]["id"]
            for name, args in (("memory_lookup", {"subject": "svc"}), ("memory_bundle", {}),
                               ("memory_get", {"item_id": item}), ("memory_history", {"item_id": item}),
                               ("memory_pin", {"item_id": item}), ("memory_unpin", {"item_id": item}),
                               ("memory_retract", {"item_id": item, "rev": 1, "request_id": "f2"}),
                               ("memory_forget", {"item_id": item, "request_id": "f3"})):
                out[name] = (await session.call_tool(name, args)).structuredContent
            return out
        out = run_mcp(self.path, self.tokens["alice"], calls)
        for name, envelope in out.items():
            self.assertTrue(envelope["ok"], (name, envelope))
            self.assertEqual(envelope["op"], server.operation_of(name))
        self.assertIn("not_guaranteed", out["memory_forget"])
        self.assertEqual(out["memory_bundle"]["estimator"], "bytes3-v1")


class StdioTests(McpCase):
    def test_stdio_transport_over_pipes(self):
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        token_file = write_token(self.dir / "alice.token", self.tokens["alice"])
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = {"RECALL_TOKEN_FILE": str(token_file), "PYTHONPATH": repo, "PYTHONDONTWRITEBYTECODE": "1",
               "PATH": os.environ.get("PATH", "")}
        params = StdioServerParameters(command=sys.executable, args=["-m", "recall_v2", "mcp", "--store",
                                                                     str(self.path)], env=env, cwd=repo)

        async def main():
            with anyio.fail_after(60):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        init = await session.initialize()
                        tools = await session.list_tools()
                        saved = await session.call_tool("memory_save", {"kind": "note", "request_id": "s1",
                                                                        "key": "k", "body": "over stdio"})
                        found = await session.call_tool("memory_lookup", {"query": "stdio"})
                        return init, tools, saved, found
        init, tools, saved, found = anyio.run(main)
        self.assertEqual(init.serverInfo.name, "recall-v2")
        self.assertEqual(sorted(t.name for t in tools.tools), sorted(SPEC_TOOLS))
        self.assertEqual(saved.structuredContent["results"][0]["scope"], {"type": "private", "id": "alice"})
        self.assertEqual(found.structuredContent["count"], 1)
        self.assertEqual(found.content[0].text, contract.to_text(found.structuredContent))


if __name__ == "__main__":
    unittest.main()


class SharedServiceTests(StoreTestCase):
    """One shared token serves every bot; the `bot` argument selects that bot's memory."""

    def setUp(self):
        self.dir = self.private_dir()
        self.path = self.dir / "store.sqlite3"
        tokens = setup_store(self.path, principals=("shared-service", "alice"), members=("alice",))
        self.shared, self.alice = tokens["shared-service"], tokens["alice"]
        self.store = Store.open(self.path)
        self.addCleanup(self.store.close)

    def call(self, name, arguments, token):
        return server.handle(self.store, name, arguments, token)

    def test_bot_name_keeps_memory_separate_under_one_token(self):
        saved = self.call("memory_save", {"kind": "note", "request_id": "s1", "key": "k", "body": "alpha note",
                                          "scope": "private", "bot": "alpha"}, self.shared)
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["results"][0]["scope"], {"type": "private", "id": "alpha"})
        item_id = saved["results"][0]["id"]
        mine = self.call("memory_lookup", {"query": "alpha note", "bot": "alpha"}, self.shared)
        self.assertEqual([r["id"] for r in mine["results"]], [item_id])
        other = self.call("memory_lookup", {"query": "alpha note", "bot": "beta"}, self.shared)
        self.assertEqual(other["results"], [])
        self.assertFalse(self.call("memory_get", {"item_id": item_id, "bot": "beta"}, self.shared)["ok"])
        forgotten = self.call("memory_forget", {"item_id": item_id, "request_id": "f1", "bot": "alpha"}, self.shared)
        self.assertTrue(forgotten["ok"], forgotten)
        self.assertEqual(self.call("memory_lookup", {"query": "alpha note", "bot": "alpha"}, self.shared)["results"], [])

    def test_a_bot_row_left_by_an_earlier_token_is_rekeyed(self):
        with write_transaction(self.store.db) as db:
            db.execute("INSERT INTO principals (id, token_digest, created_at, revoked_at) VALUES (?, NULL, 1, 1)",
                       ("legacy-bot",))
        saved = self.call("memory_save", {"kind": "note", "request_id": "r1", "key": "k", "body": "legacy note",
                                          "scope": "private", "bot": "legacy-bot"}, self.shared)
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["results"][0]["scope"], {"type": "private", "id": "legacy-bot"})

    def test_bot_argument_rules(self):
        missing = self.call("memory_lookup", {"query": "x"}, self.shared)
        self.assertEqual(missing["error"]["code"], "invalid_input")
        bad_name = self.call("memory_lookup", {"query": "x", "bot": "bad name"}, self.shared)
        self.assertEqual(bad_name["error"]["code"], "invalid_input")
        private_token = self.call("memory_lookup", {"query": "x", "bot": "alpha"}, self.alice)
        self.assertEqual(private_token["error"]["code"], "invalid_input")
        own = self.call("memory_lookup", {"query": "x"}, self.alice)
        self.assertTrue(own["ok"], own)
        self.assertEqual(self.call("memory_lookup", {"query": "x"}, "not-a-token")["error"]["code"], "unauthorized")
