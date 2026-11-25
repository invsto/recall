"""Acceptance (d): MCP result shapes are byte-identical to the pre-rename code.

tests/fixtures/mcp_result_shapes.json was recorded from the original OpenBot-named code at commit 1777310
by running the same scenario (tests/service_shape.py). This test replays it against the current code.
It calls the same invoke() the MCP adapter uses, and wraps results the way the adapter does.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

from recall_core.service import mcp
from recall_core.service.store import ServiceStore, StoreError, canonical_json, credential_digest
from .service_shape import record

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_result_shapes.json"


class ServiceResultShapeTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the service extra for MCP shape checks")
    def test_results_match_the_pre_rename_recording(self):
        expected = json.loads(FIXTURE.read_text())
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            store = ServiceStore(Path(directory) / "store.sqlite3")
            actual = record(
                store, store_error=StoreError, invoke=mcp.invoke, canonical_json=canonical_json,
                storage_error=mcp.STORAGE_ERROR, invalid_input=mcp.INVALID_INPUT,
                credential_digest=credential_digest, add_action="principal-add",
            )
        self.assertEqual(len(actual), len(expected))
        for got, want in zip(actual, expected):
            self.assertEqual(got, want, want["operation"])


if __name__ == "__main__":
    unittest.main()
