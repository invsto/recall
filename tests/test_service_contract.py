"""MCP contract snapshot: tool names, descriptions, and input schemas match the pre-rename code.

The snapshot was generated from the original OpenBot-named module at HEAD. The server name is the
one intended change in this release: "recall-openbot" becomes "recall-service".
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest import mock
import unittest

HAS_MCP = importlib.util.find_spec("mcp") is not None
SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "mcp_contract.json"


@unittest.skipUnless(HAS_MCP, "install the service extra for MCP contract checks")
class McpContractTests(unittest.TestCase):
    def test_tools_match_the_snapshot(self):
        from recall_core.service import mcp as service_mcp

        expected = json.loads(SNAPSHOT.read_text())
        actual = []
        for name, (description, schema) in service_mcp.TOOL_SPECS.items():
            canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
            actual.append({
                "name": f"recall_{name}",
                "description": description,
                "input_schema_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            })
        self.assertEqual(actual, expected["tools"])

    def test_server_name_is_recall_service(self):
        from recall_core.service import mcp as service_mcp
        from recall_core.service.store import ServiceStore

        expected = json.loads(SNAPSHOT.read_text())["server_name"]
        self.assertEqual(expected, "recall-service")
        with tempfile.TemporaryDirectory() as directory:
            store = ServiceStore(Path(directory) / "service.sqlite3")
            with mock.patch.object(service_mcp, "Server", wraps=service_mcp.Server) as server_class:
                service_mcp.create_app(store, hosts=["127.0.0.1"], origins=[])
        server_class.assert_called_once_with(expected)


if __name__ == "__main__":
    unittest.main()
