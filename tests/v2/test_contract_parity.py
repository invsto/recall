"""M6 contract_parity: the library, the CLI and the MCP server return the same canonical JSON for a fixed
scenario (spec 4.4: "All interfaces return identical canonical JSON"), after masking timestamps, ULIDs
and digests. A second test checks byte-for-byte identity, unmasked, on one store with a frozen clock."""
from __future__ import annotations

import json
import unittest

from recall_v2.api import Client
from recall_v2.api.contract import to_text

from .helpers import StoreTestCase, T0
from .interface_helpers import Drivers, run_cli, run_mcp, scenario, setup_store, write_token


class ContractParityTests(StoreTestCase):
    def fresh(self):
        directory = self.private_dir()
        path = directory / "store.sqlite3"
        token = setup_store(path)["alice"]
        return directory, path, token

    def test_library_cli_and_mcp_envelopes_are_identical(self):
        directory, path, token = self.fresh()
        library = scenario(Drivers.library(path, token))
        directory, path, token = self.fresh()
        cli = scenario(Drivers.cli(path, write_token(directory / "alice.token", token)))
        directory, path, token = self.fresh()
        mcp = scenario(Drivers.mcp(path, token))
        self.assertEqual(len(library), 22)
        for lib, via_cli, via_mcp in zip(library, cli, mcp):
            self.assertEqual(lib, via_cli, lib["op"])
            self.assertEqual(lib, via_mcp, lib["op"])
        codes = [json.loads(s["text"]).get("error", {}).get("code") for s in library]
        self.assertEqual(codes[-5:], ["invalid_input", "not_found", "request_id_conflict", "conflict", "not_found"])
        self.assertTrue(all(code is None for code in codes[:-5]), codes)

    def test_unmasked_bytes_are_identical_on_one_store(self):
        directory = self.private_dir()
        path = directory / "store.sqlite3"
        token = setup_store(path, clock=lambda: T0)["alice"]
        token_file = write_token(directory / "alice.token", token)
        with Client.open(path, token, clock=lambda: T0) as client:
            saved = client.save(kind="fact", request_id="r1",
                                fact={"subject": "svc", "predicate": "region", "value": {"zone": "eu-1", "n": 2}})
            self.assertTrue(saved["ok"], saved)
            item_id = saved["results"][0]["id"]
        when = "2026-11-25T09:30:00.000000Z"
        calls = [("lookup", {"subject": "svc", "as_of": when, "known_at": when}),
                 ("lookup", {"query": "svc region", "as_of": when, "known_at": when, "explain": True}),
                 ("bundle", {"as_of": when, "budget_tokens": 400}),
                 ("save", {"kind": "fact", "request_id": "r1",
                           "fact": {"subject": "svc", "predicate": "region", "value": {"zone": "eu-1", "n": 2}}}),
                 ("lookup", {"query": "x", "subject": "svc"}),
                 ("history", {"item_id": "missing"})]
        with Client.open(path, token) as client:
            expected = [to_text(getattr(client, op)(**args)) for op, args in calls]
        for (op, args), text in zip(calls, expected):
            status, out = run_cli([op, "--store", path, "--token-file", token_file, "--args", json.dumps(args)])
            self.assertEqual(out, text + "\n", op)

            async def one(session, op=op, args=args):
                result = await session.call_tool("memory_" + op, args)
                return result.content[0].text
            self.assertEqual(run_mcp(path, token, one), text, op)
        self.assertIn(item_id, expected[0])


if __name__ == "__main__":
    unittest.main()
