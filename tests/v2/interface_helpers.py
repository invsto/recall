"""Shared helpers for the M6 interface tests: drive the library, the CLI and the MCP server the same way.

No sockets: the CLI runs in-process (or as a subprocess over pipes), the MCP server through the SDK's
in-memory transport.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import re

from recall_v2 import cli
from recall_v2.api import Client, Operator

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
HEX32 = re.compile(r"[0-9a-f]{32,}")


def mask(text: str) -> str:
    """Mask the values that change on every run: timestamps, ULIDs and hex digests (the same idea as the
    v1 shape test, adapted to v2's RFC 3339 `Z` timestamps and 26-character ULIDs)."""
    return HEX32.sub("<hex>", ULID.sub("<id>", TIMESTAMP.sub("<ts>", text)))


def setup_store(path: Path, principals=("alice",), project="proj", members=("alice",), clock=None) -> dict:
    """Create a store with principals, one project and memberships; return {principal: token}."""
    with Operator.open(path, create=True, clock=clock) as operator:
        tokens = {}
        for name in principals:
            envelope = operator.principal_add(name)
            assert envelope["ok"], envelope
            tokens[name] = envelope["results"][0]["token"]
        if project:
            assert operator.project_add(project)["ok"]
            for name in members:
                assert operator.member_add(name, project)["ok"]
    return tokens


def write_token(path: Path, token: str) -> Path:
    from recall_v2.api.tokens import write_token_file
    return write_token_file(path, token)


def run_cli(argv) -> tuple[int, str]:
    """Run the CLI in-process; return (exit status, stdout text)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = cli.main([str(a) for a in argv])
    return status, out.getvalue()


def run_mcp(store_path: Path, token, calls):
    """Open the store on this thread, run `calls(session)` (an async function) over the in-memory
    transport and return its result."""
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session

    from recall_v2 import mcp as server
    from recall_v2.store import Store

    store = Store.open(store_path)
    try:
        async def main():
            async with create_connected_server_and_client_session(
                    server.build_server(store, server.fixed_token(token))) as session:
                return await calls(session)
        return anyio.run(main)
    finally:
        store.close()


class Drivers:
    """One `call(operation, arguments) -> canonical text` per interface, each on its own store."""

    @staticmethod
    def library(path: Path, token: str):
        from recall_v2.api.contract import to_text

        def call(op, args):
            with Client.open(path, token) as client:
                return to_text(getattr(client, op)(**args))
        return call

    @staticmethod
    def cli(path: Path, token_file: Path):
        def call(op, args):
            status, text = run_cli([op, "--store", path, "--token-file", token_file, "--args", json.dumps(args)])
            envelope = json.loads(text)
            assert status == (0 if envelope["ok"] else 1), (status, text)
            assert text.endswith("\n") and text.count("\n") == 1
            return text[:-1]
        return call

    @staticmethod
    def mcp(path: Path, token: str):
        def call(op, args):
            async def calls(session):
                result = await session.call_tool("memory_" + op, args)
                text = result.content[0].text
                assert len(result.content) == 1
                assert result.structuredContent == json.loads(text)
                assert result.isError == (not json.loads(text)["ok"])
                return text
            return run_mcp(path, token, calls)
        return call


def scenario(call) -> list[dict]:
    """A fixed principal scenario covering every operation and several error codes. Returns one
    {operation, text} per step, with volatile values masked."""
    steps = []

    def step(op, args):
        text = call(op, args)
        steps.append({"op": op, "args": mask(json.dumps(args, sort_keys=True)), "text": mask(text)})
        return json.loads(text)

    note = step("save", {"kind": "note", "request_id": "r1", "key": "release", "title": "Release checklist",
                         "body": "Ship on friday after the smoke tests."})["results"][0]
    fact = step("save", {"kind": "fact", "request_id": "r2",
                         "fact": {"subject": "service-x", "predicate": "owner", "value": "alice"}})["results"][0]
    step("save", {"kind": "fact", "request_id": "r3",
                  "fact": {"subject": "service-x", "predicate": "owner", "value": "bob"}})
    step("save", {"kind": "note", "request_id": "r1", "key": "release", "title": "Release checklist",
                  "body": "Ship on friday after the smoke tests."})  # replay
    step("lookup", {"query": "friday smoke"})
    step("lookup", {"subject": "service-x"})
    step("lookup", {"query": "friday", "explain": True, "limit": 5})
    step("get", {"item_id": note["id"]})
    step("history", {"item_id": fact["id"]})
    step("pin", {"item_id": note["id"]})
    step("bundle", {"budget_tokens": 600})
    step("unpin", {"item_id": note["id"]})
    step("retract", {"item_id": fact["id"], "rev": 2, "request_id": "r4"})
    step("lookup", {"subject": "service-x", "predicate": "owner"})
    step("save", {"kind": "note", "request_id": "r5", "scope": "project", "project": "proj", "key": "plan",
                  "title": "Plan", "body": "Project plan draft."})
    step("bundle", {"project": "proj"})
    step("forget", {"item_id": note["id"], "request_id": "r6"})
    # error paths
    step("lookup", {"query": "friday", "limit": 99})                                   # invalid_input
    step("get", {"item_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"})                            # not_found
    step("save", {"kind": "note", "request_id": "r2", "key": "other", "body": "x"})   # request_id_conflict
    step("save", {"kind": "note", "request_id": "r7", "key": "fresh", "body": "x", "expected_revision": 3})  # conflict
    step("bundle", {"project": "nope"})                                               # not_found
    return steps
