"""Exercise the operator CLI and official MCP client against a real local listener."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if importlib.util.find_spec("mcp") is not None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import Implementation


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN = ROOT / "docs" / "examples" / "structured-context.md"


@unittest.skipUnless(importlib.util.find_spec("mcp"), "install the openbot extra for MCP integration")
class OpenBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.db = self.directory / "state" / "openbot.sqlite3"
        self.cli("init")
        self.alpha = self.cli("bot-add", "alpha")["token"]
        self.beta = self.cli("bot-add", "beta")["token"]
        self.outside = self.cli("bot-add", "outside")["token"]

    def cli(self, *args, db=None, success=True):
        result = subprocess.run(
            [sys.executable, str(ROOT / "recall.py"), "openbot", "--db", str(db or self.db), *args],
            cwd=self.directory,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        # The deprecated `recall openbot` alias prints a warning line on stderr before the JSON.
        stream = result.stdout if success else result.stderr
        json_lines = [line for line in stream.splitlines() if line.startswith("{")]
        return json.loads(json_lines[-1] if json_lines else stream)

    @contextlib.contextmanager
    def server(self, *, redact=()):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryFile() as logs, subprocess.Popen(
            [sys.executable, str(ROOT / "recall.py"), "openbot", "--db", str(self.db), "serve",
             "--port", str(port)],
            cwd=self.directory,
            stdout=logs,
            stderr=logs,
        ) as process:
            try:
                for _ in range(150):
                    if process.poll() is not None:
                        break
                    try:
                        with urlopen(base + "/health", timeout=0.3) as response:
                            self.assertEqual(json.load(response), {"status": "ok"})
                        break
                    except OSError:
                        time.sleep(0.03)
                else:
                    self.fail("service readiness timed out")
                if process.poll() is not None:
                    logs.seek(0)
                    self.fail(logs.read().decode())
                yield base
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if redact:
                    logs.seek(0)
                    contents = logs.read().decode()
                    for value in redact:
                        self.assertNotIn(value, contents)

    @contextlib.asynccontextmanager
    async def client(self, base, token, reported_name="synthetic-client"):
        from httpx import AsyncClient

        async with AsyncClient(headers={"Authorization": f"Bearer {token}"}, trust_env=False) as http_client:
            async with streamable_http_client(base + "/mcp", http_client=http_client) as (read, write, _):
                info = Implementation(name=reported_name, version="1")
                async with ClientSession(read, write, client_info=info) as session:
                    await session.initialize()
                    yield session

    async def tool(self, session, tool_name, **arguments):
        result = await session.call_tool("recall_" + tool_name, arguments)
        self.assertIsInstance(result.structuredContent, dict, result.content)
        return result.structuredContent

    def rejected_http(self, base, headers, status, data=None):
        request = Request(base + "/mcp", data=data, headers=headers)
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, status)
        self.assertEqual(error.exception.headers.get("Cache-Control"), "no-store")

    def checkpoint(self, key="restart", expected_revision=0, request_id="save-one", summary="alpha-only cobalt"):
        return {
            "scope": "task",
            "key": key,
            "expected_revision": expected_revision,
            "request_id": request_id,
            "checkpoint": {
                "summary": summary,
                "decisions": ["Use the synthetic fixture."],
                "questions": ["Did it restart?"],
                "actions": ["Confirm the model."],
                "source_refs": ["https://example.test/test-run"],
            },
        }

    async def test_private_checkpoint_survives_restart_and_ignores_client_identity(self):
        with self.server() as base:
            self.rejected_http(base, {}, 401)
            self.rejected_http(base, {"Authorization": "Bearer example-invalid-token"}, 401)
            async with self.client(base, self.alpha) as alpha:
                listed = await alpha.list_tools()
                self.assertIn("recall_checkpoint_save", {tool.name for tool in listed.tools})
                saved = await self.tool(alpha, "checkpoint_save", **self.checkpoint())
                self.assertTrue(saved["ok"])
            async with self.client(base, self.beta, reported_name="alpha") as beta:
                inaccessible = await self.tool(beta, "checkpoint_get", scope="task", key="restart")
                self.assertEqual(inaccessible, {"ok": True, "revision": 0, "checkpoint": None})
                spoofed = await self.tool(beta, "checkpoint_get", scope="task", key="restart", bot="alpha")
                self.assertEqual(spoofed["error"], "invalid_input")
                private = await self.tool(beta, "checkpoint_save", **self.checkpoint(summary="beta's checkpoint"))
                self.assertEqual(private["revision"], 1)
        with self.server() as base:
            async with self.client(base, self.alpha) as alpha:
                loaded = await self.tool(alpha, "checkpoint_get", scope="task", key="restart")
                self.assertEqual(loaded["revision"], saved["revision"])
                self.assertEqual(loaded["checkpoint"], self.checkpoint()["checkpoint"])
            async with self.client(base, self.outside) as outsider:
                empty = await self.tool(outsider, "checkpoint_get", scope="task", key="restart")
                self.assertEqual(empty["revision"], 0)
                self.assertIsNone(empty["checkpoint"])

    async def test_concurrent_saves_retry_and_credential_lifecycle(self):
        with self.server() as base:
            async with self.client(base, self.alpha) as first, self.client(base, self.alpha) as second:
                one = self.checkpoint(request_id="request-one")
                two = self.checkpoint(request_id="request-two", summary="parallel proposal")
                outcomes = await asyncio.gather(
                    self.tool(first, "checkpoint_save", **one),
                    self.tool(second, "checkpoint_save", **two),
                )
                winner, loser = sorted(outcomes, key=lambda item: not item["ok"])
                self.assertTrue(winner["ok"])
                self.assertEqual(loser["error"], "conflict")
                self.assertEqual(loser["current_revision"], winner["revision"])
                saved = one if outcomes[0]["ok"] else two
                self.assertEqual(await self.tool(first, "checkpoint_save", **saved), winner)
                different = {**saved, "key": "another-task"}
                reused = await self.tool(first, "checkpoint_save", **different)
                self.assertEqual(reused["error"], "request_id_conflict")
                later = await self.tool(second, "checkpoint_save", **self.checkpoint(
                    request_id="later", expected_revision=winner["revision"], summary="new checkpoint",
                ))
                self.assertGreater(later["revision"], winner["revision"])
                self.assertEqual(await self.tool(first, "checkpoint_save", **saved), winner)
                current = await self.tool(second, "checkpoint_get", scope="task", key="restart")
                self.assertEqual(current["checkpoint"]["summary"], "new checkpoint")

            rotated = self.cli("bot-rotate", "alpha")["token"]
            self.assertNotEqual(rotated, self.alpha)
            self.rejected_http(base, {"Authorization": f"Bearer {self.alpha}"}, 401)
            async with self.client(base, rotated) as alpha:
                restored = await self.tool(alpha, "checkpoint_get", scope="task", key="restart")
                self.assertEqual(restored["checkpoint"]["summary"], "new checkpoint")
            self.cli("bot-revoke", "alpha")
            self.rejected_http(base, {"Authorization": f"Bearer {rotated}"}, 401)

    async def test_project_briefs_and_excerpts_require_separate_review_and_current_membership(self):
        self.cli("project-add", "pilot")
        self.cli("member-grant", "pilot", "alpha")
        self.cli("member-grant", "pilot", "beta")
        markdown = MARKDOWN.read_text(encoding="utf-8")
        with self.server() as base:
            async with (
                self.client(base, self.alpha) as alpha,
                self.client(base, self.beta) as beta,
                self.client(base, self.outside) as outside,
            ):
                proposal_params = dict(project="pilot", key="restart", expected_revision=0,
                                       request_id="brief-one", markdown=markdown)
                forbidden = await self.tool(outside, "brief_propose", **proposal_params)
                self.assertEqual(forbidden["error"], "not_found")
                brief = await self.tool(alpha, "brief_propose", **proposal_params)
                self.assertEqual(brief["status"], "pending")
                self.assertEqual(await self.tool(alpha, "brief_propose", **proposal_params), brief)
                invalid = await self.tool(alpha, "brief_propose", **{**proposal_params, "markdown": "# Invalid"})
                self.assertEqual(invalid["error"], "invalid_input")
                competing = await self.tool(beta, "brief_propose", **{**proposal_params, "request_id": "competing"})
                excerpt = await self.tool(alpha, "excerpt_propose", project="pilot", key="restart",
                                          expected_revision=0, request_id="excerpt-one",
                                          excerpt="Fictional batch-job line.", source_ref="https://example.test/logs#batch")
                self.assertEqual((await self.tool(beta, "brief_get", project="pilot", key="restart"))["error"],
                                 "not_found")
                reviewable = self.cli("proposal-show", str(brief["proposal_id"]))
                self.assertEqual(reviewable["markdown"], markdown)
                self.assertEqual(reviewable["author"], "alpha")
                self.assertEqual(self.cli("proposal-list", "pilot")["proposals"][0]["status"], "pending")
                self.cli("review", str(brief["proposal_id"]), "approve", "--reviewer", "operator-one")
                self.assertEqual(self.cli("review", str(competing["proposal_id"]), "approve",
                                          "--reviewer", "operator-one", success=False)["error"], "conflict")
                self.cli("review", str(competing["proposal_id"]), "reject", "--reviewer", "operator-one")
                accepted = await self.tool(beta, "brief_get", project="pilot", key="restart")
                self.assertEqual(accepted["markdown"], markdown)
                self.assertEqual(accepted["author"], "alpha")
                self.assertEqual(accepted["reviewer"], "operator-one")
                self.assertEqual(accepted["previous_revision"], 0)
                self.assertEqual((await self.tool(outside, "brief_get", project="pilot", key="restart"))["error"],
                                 "not_found")
                self.assertEqual((await self.tool(beta, "excerpt_get", project="pilot", key="restart"))["error"],
                                 "not_found")
                self.cli("review", str(excerpt["proposal_id"]), "approve", "--reviewer", "operator-two")
                excerpt_read = await self.tool(beta, "excerpt_get", project="pilot", key="restart")
                self.assertEqual(excerpt_read["source_ref"], "https://example.test/logs#batch")
                self.assertEqual(excerpt_read["reviewer"], "operator-two")
                export = self.directory / "accepted.md"
                self.cli("export", "pilot", "restart", str(export))
                self.assertEqual(export.read_bytes(), MARKDOWN.read_bytes())
                self.assertEqual(export.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.cli("export", "pilot", "restart", str(export), success=False)["error"],
                                 "conflict")
                self.assertEqual(export.read_bytes(), MARKDOWN.read_bytes())
                pending = await self.tool(beta, "brief_propose", **{
                    **proposal_params, "expected_revision": accepted["revision"], "request_id": "next-brief",
                })
                self.cli("member-revoke", "pilot", "beta")
                self.assertEqual(self.cli("review", str(pending["proposal_id"]), "approve",
                                          "--reviewer", "operator-one", success=False)["error"], "not_found")
                for name in ("brief_get", "excerpt_get"):
                    self.assertEqual((await self.tool(beta, name, project="pilot", key="restart"))["error"],
                                     "not_found")
                still_pending = await self.tool(alpha, "brief_propose", **{
                    **proposal_params, "expected_revision": accepted["revision"], "request_id": "alpha-next",
                })
                self.cli("prune", "--publication-days", "0", "--apply")
                after_retention = self.cli("proposal-show", still_pending["proposal_id"])
                self.assertEqual(after_retention["status"], "pending")
                self.assertEqual(after_retention["markdown"], markdown)
                stale = self.cli("review", still_pending["proposal_id"], "approve",
                                 "--reviewer", "operator-one", success=False)
                self.assertEqual(stale["error"], "conflict")
                gone = await self.tool(alpha, "brief_get", project="pilot", key="restart")
                self.assertEqual(gone["error"], "not_found")
                self.assertEqual(gone["current_revision"], stale["current_revision"])

    async def test_search_delete_retention_and_backup_restore(self):
        self.cli("project-add", "pilot")
        self.cli("member-grant", "pilot", "alpha")
        self.cli("member-grant", "pilot", "beta")
        with self.server() as base:
            async with (
                self.client(base, self.alpha) as alpha,
                self.client(base, self.beta) as beta,
                self.client(base, self.outside) as outside,
            ):
                first = self.checkpoint(summary="alpha cobalt checkpoint")
                first_revision = (await self.tool(alpha, "checkpoint_save", **first))["revision"]
                update = self.checkpoint(expected_revision=first_revision, request_id="later",
                                         summary="updated Cobalt checkpoint")
                second_revision = (await self.tool(alpha, "checkpoint_save", **update))["revision"]
                self.cli("prune", "--apply")
                self.assertEqual((await self.tool(alpha, "checkpoint_save", **first))["revision"], first_revision)
                old = await self.tool(alpha, "checkpoint_get", scope="task", key="restart", revision=first_revision)
                self.assertEqual(old["checkpoint"]["summary"], first["checkpoint"]["summary"])
                self.assertEqual((await self.tool(beta, "checkpoint_get", scope="task", key="restart",
                                                  revision=first_revision))["error"], "not_found")
                shared = dict(project="pilot", key="notes", expected_revision=0, request_id="shared",
                              excerpt="Shared cobalt excerpt.", source_ref="https://example.test/issue#cobalt")
                proposal = await self.tool(alpha, "excerpt_propose", **shared)
                self.assertEqual((await self.tool(beta, "project_search", query="cobalt"))["matches"], [])
                self.cli("review", str(proposal["proposal_id"]), "approve", "--reviewer", "operator-one")

                def kinds(result):
                    return {match["kind"] for match in result["matches"]}

                self.assertEqual(kinds(await self.tool(alpha, "project_search", query="cobalt")),
                                 {"checkpoint", "excerpt"})
                self.assertEqual(kinds(await self.tool(alpha, "project_search", query="cobalt", project="pilot")),
                                 {"excerpt"})
                self.assertEqual(kinds(await self.tool(beta, "project_search", query="cobalt")), {"excerpt"})
                self.assertEqual(kinds(await self.tool(outside, "project_search", query="cobalt")), set())
                self.cli("member-revoke", "pilot", "beta")
                self.assertEqual(kinds(await self.tool(beta, "project_search", query="cobalt")), set())

                backup = self.directory / "backups" / "snapshot.sqlite3"
                self.cli("backup", str(backup))
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.cli("backup", str(backup), success=False)["error"], "conflict")

                self.cli("checkpoint-delete", "alpha", "task", "restart")
                deleted = await self.tool(alpha, "checkpoint_get", scope="task", key="restart")
                self.assertIsNone(deleted["checkpoint"])
                self.assertGreater(deleted["revision"], second_revision)
                for stale in (first, update):
                    replay = await self.tool(alpha, "checkpoint_save", **stale)
                    self.assertEqual(replay["error"], "conflict")
                    self.assertEqual(replay["current_revision"], deleted["revision"])
                self.assertEqual((await self.tool(alpha, "checkpoint_get", scope="task", key="restart",
                                                  revision=first_revision))["error"], "not_found")
                recreation = self.checkpoint(expected_revision=deleted["revision"], request_id="recreate")
                recreated = await self.tool(alpha, "checkpoint_save", **recreation)
                self.assertTrue(recreated["ok"])
                self.cli("publication-delete", "pilot", "excerpt", "notes")
                self.assertEqual((await self.tool(alpha, "excerpt_propose", **shared))["error"], "conflict")
                self.assertEqual(kinds(await self.tool(alpha, "project_search", query="cobalt")), {"checkpoint"})
                self.cli("prune", "--checkpoint-days", "0", "--apply")
                self.assertEqual(kinds(await self.tool(alpha, "project_search", query="cobalt")), set())
                self.assertEqual((await self.tool(alpha, "checkpoint_save", **recreation))["error"], "conflict")
                audit = json.dumps(self.cli("audit", "--limit", "100"))
                self.assertIn("checkpoint-delete", audit)
                self.assertNotIn("cobalt", audit.casefold())
                self.assertNotIn(self.alpha, audit)

        restored = self.directory / "restored" / "openbot.sqlite3"
        self.cli("restore", str(backup), db=restored)
        self.assertEqual(self.cli("restore", str(backup), db=restored, success=False)["error"], "conflict")
        self.assertEqual(self.cli("health", db=restored), {"status": "ok"})
        self.db = restored
        with self.server() as base:
            self.rejected_http(base, {"Authorization": f"Bearer {self.alpha}"}, 401)
            token = self.cli("bot-rotate", "alpha")["token"]
            async with self.client(base, token) as alpha:
                current = await self.tool(alpha, "checkpoint_get", scope="task", key="restart")
                self.assertEqual(current["revision"], second_revision)
                self.assertEqual(current["checkpoint"]["summary"], "updated Cobalt checkpoint")
                self.assertEqual(kinds(await self.tool(alpha, "project_search", query="cobalt")),
                                 {"checkpoint", "excerpt"})

    async def test_transport_and_tool_limits_do_not_log_inputs(self):
        synthetic = "placeholder-sensitive-input"
        with self.server(redact=(synthetic, self.alpha)) as base:
            bearer = {"Authorization": f"Bearer {self.alpha}"}
            self.rejected_http(base, {**bearer, "Host": synthetic}, 421)
            self.rejected_http(base, {**bearer, "Origin": f"https://{synthetic}.test"}, 403)
            headers = {**bearer, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            self.rejected_http(base, headers, 413, b"x" * (128 * 1024 + 1))
            async with self.client(base, self.alpha) as alpha:
                oversized = await self.tool(alpha, "checkpoint_save", **self.checkpoint(summary="😀" * 2100))
                self.assertEqual(oversized["error"], "invalid_input")
                invalid = await self.tool(alpha, "checkpoint_get", scope="task", key="invalid\n")
                self.assertEqual(invalid["error"], "invalid_input")
                unknown = await self.tool(alpha, synthetic)
                self.assertEqual(unknown["error"], "invalid_input")
