"""M6 CLI (spec 7.5): every listed command exists and prints one envelope as canonical JSON; exit 0 when
`ok`, 1 when `ok: false`, 2 for usage errors. Runs in-process, plus one real `python -m recall_v2`
subprocess over pipes. The `serve` command is never started here (it needs a loopback socket)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from recall_v2 import cli
from recall_v2.api.contract import to_text

from .extraction_helpers import BASIC_LOG, write_log
from .helpers import StoreTestCase
from .interface_helpers import run_cli, setup_store, write_token

REPO = Path(__file__).resolve().parents[2]

# Spec 7.5, transcribed (sub-actions as separate entries).
SPEC_COMMANDS = [
    "save", "retract", "lookup", "bundle", "get", "history", "forget", "pin", "unpin",
    "init", "migrate", "principal add", "principal rotate", "principal revoke", "principal list",
    "project add", "project list", "member add", "member remove", "member list",
    "review list", "review approve", "review reject", "review edit", "conflict list", "conflict resolve",
    "source add", "source list", "source delete", "extract plan", "extract run", "extract cancel", "extract list",
    "delete", "expire-check", "maintain", "backup", "restore", "export", "audit verify", "doctor", "mcp", "serve",
]


def subcommands(parser) -> dict:
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return action.choices
    return {}


class CliCase(StoreTestCase):
    def setUp(self):
        self.dir = self.private_dir()
        self.path = self.dir / "store.sqlite3"

    def cli(self, *argv, store=True, expect=0) -> dict:
        args = list(argv) + (["--store", str(self.path)] if store else [])
        status, out = run_cli(args)
        self.assertTrue(out.endswith("\n") and out.count("\n") == 1, out)
        envelope = json.loads(out)
        self.assertEqual(out[:-1], to_text(envelope), "not canonical JSON")
        self.assertEqual(envelope["schema_version"], "recall.v2")
        self.assertIn("op", envelope)
        self.assertEqual(status, expect, out)
        self.assertEqual(envelope["ok"], expect == 0, out)
        if not envelope["ok"]:
            self.assertEqual(set(envelope["error"]), {"code", "message", "details"})
        return envelope

    def setup_alice(self):
        self.cli("init")
        self.cli("principal", "add", "alice", "--token-out", self.dir / "alice.token")
        self.cli("project", "add", "proj")
        self.cli("member", "add", "alice", "proj")
        return self.dir / "alice.token"


class CommandListTests(CliCase):
    def test_every_spec_command_exists(self):
        parser = cli.build_parser()
        top = subcommands(parser)
        for command in SPEC_COMMANDS:
            words = command.split()
            self.assertIn(words[0], top, command)
            if len(words) > 1:
                self.assertIn(words[1], subcommands(top[words[0]]), command)
        self.assertIn("migrate-v1", top)

    def test_client_commands_never_take_a_principal(self):
        top = subcommands(cli.build_parser())
        for name in ("save", "retract", "lookup", "bundle", "get", "history", "forget", "pin", "unpin"):
            options = {o for a in top[name]._actions for o in a.option_strings}
            self.assertFalse({"--principal", "--principal-id", "--token", "--owner", "--actor"} & options, name)
            self.assertIn("--token-file", options)


class OperatorCommandTests(CliCase):
    def test_setup_and_lifecycle_commands_print_envelopes(self):
        token_file = self.setup_alice()
        init_again = self.cli("init")
        self.assertFalse(init_again["results"][0]["created"])
        self.assertEqual(self.cli("principal", "list")["results"][0]["principal_id"], "alice")
        self.assertEqual(self.cli("project", "list")["count"], 1)
        self.assertEqual(self.cli("member", "list", "--project", "proj")["count"], 1)
        rotated = self.cli("principal", "rotate", "alice")
        self.assertEqual(len(rotated["results"][0]["token"]), 43)
        self.cli("lookup", "--query", "x", "--token-file", token_file, expect=1)   # old token is dead
        os.unlink(token_file)
        write_token(token_file, rotated["results"][0]["token"])

        saved = self.cli("save", "--token-file", token_file, "--kind", "note", "--request-id", "r1", "--scope",
                         "project", "--project", "proj", "--key", "plan", "--title", "Plan", "--body", "draft")
        item = saved["results"][0]["id"]
        self.assertEqual(saved["results"][0]["state"], "candidate")
        listed = self.cli("review", "list")
        self.assertEqual(listed["results"][0]["item_id"], item)
        self.cli("review", "approve", item, "1")
        self.cli("review", "approve", item, "1", expect=1)
        self.cli("save", "--token-file", token_file, "--kind", "note", "--request-id", "r2", "--scope", "project",
                 "--project", "proj", "--key", "plan", "--title", "Plan", "--body", "second")
        self.cli("review", "edit", item, "2", "--body", "edited")
        self.cli("save", "--token-file", token_file, "--kind", "note", "--request-id", "r3", "--scope", "project",
                 "--project", "proj", "--key", "plan", "--title", "Plan", "--body", "third")
        self.cli("review", "reject", item, "4", "--note", "no")
        self.cli("review", "list", "--stale")
        self.cli("conflict", "list")
        self.cli("conflict", "resolve", "01ARZ3NDEKTSV4RRFFQ69G5FAV", "reject", expect=1)
        self.cli("expire", item)
        self.assertEqual(self.cli("expire-check")["count"], 1)
        backup = self.cli("backup", "--prune")["results"][0]
        self.assertEqual(self.cli("restore", backup["path"], "--to", self.dir / "restored.sqlite3",
                                  expect=1)["error"]["code"], "invalid_input")   # no ledger yet
        restored = self.cli("restore", backup["path"], "--to", self.dir / "restored.sqlite3", "--no-ledger")
        self.assertTrue(restored["ok"])
        self.assertEqual(self.cli("restore", backup["path"], "--to", self.dir / "restored.sqlite3", "--no-ledger",
                                  expect=1)["error"]["code"], "backup_exists")
        self.assertEqual(self.cli("export", "--project", "proj")["results"][0]["items"], 1)
        deleted = self.cli("delete", item)
        self.assertIn("not_guaranteed", deleted)
        self.cli("maintain")
        self.assertTrue(self.cli("audit", "verify")["results"][0]["ok"])
        self.assertTrue(self.cli("doctor")["results"][0]["ok"])
        self.cli("member", "remove", "alice", "proj")
        self.cli("principal", "revoke", "alice")
        self.assertEqual(self.cli("lookup", "--query", "x", "--token-file", token_file,
                                  expect=1)["error"]["code"], "unauthorized")

    def test_source_and_extraction_commands(self):
        self.setup_alice()
        log = write_log(self.dir / "log.jsonl", BASIC_LOG)
        added = self.cli("source", "add", "--adapter", "jsonl-messages", "--config", json.dumps({"path": str(log)}),
                         "--target", json.dumps({"scope": "private", "principal": "alice"}),
                         "--endpoint", "local:rule-extractor", "--model-id", "rules-v1")
        source = added["results"][0]["source_id"]
        self.assertEqual(self.cli("source", "list")["count"], 1)
        plan = self.cli("extract", "plan", source)["results"][0]
        self.assertEqual(self.cli("extract", "run", source, "--ack", plan["manifest_hash"],
                                  expect=1)["error"]["code"], "forbidden")   # not enabled yet
        self.cli("source", "update", source, "--enable")
        plan = self.cli("extract", "plan", source)["results"][0]
        bad = self.cli("extract", "run", source, "--ack", "0" * 64, expect=1)
        self.assertEqual(bad["error"]["code"], "disclosure_required")
        ran = self.cli("extract", "run", source, "--ack", plan["manifest_hash"])["results"][0]
        self.assertEqual(ran["status"], "done")
        self.assertGreater(self.cli("review", "list")["count"], 0)
        self.assertEqual(self.cli("extract", "list", "--source", source)["count"], 1)
        self.assertEqual(self.cli("extract", "cancel", ran["run_id"])["results"][0]["status"], "done")
        self.cli("source", "update", source, "--endpoint", "local:other-model")
        plan = self.cli("extract", "plan", source)["results"][0]
        missing = self.cli("extract", "run", source, "--ack", plan["manifest_hash"], expect=1)
        self.assertEqual(missing["error"]["code"], "missing_dependency")
        self.cli("source", "delete", source, "--cascade")

    def test_migrate_reports_errors_as_envelopes(self):
        failed = self.cli("migrate", "--from", self.dir / "absent.sqlite3", "--to", self.dir / "new.sqlite3",
                          store=False, expect=1)
        self.assertEqual(failed["error"]["code"], "wrong_store")
        self.assertEqual(failed["op"], "migrate")
        self.cli("migrate-v1", "--from", self.dir / "absent.sqlite3", "--to", self.dir / "new.sqlite3",
                 "--dry-run", store=False, expect=1)


class ClientCommandTests(CliCase):
    def test_client_commands_and_exit_codes(self):
        token_file = self.setup_alice()
        saved = self.cli("save", "--token-file", token_file, "--kind", "fact", "--request-id", "f1",
                         "--fact", json.dumps({"subject": "svc", "predicate": "owner", "value": "alice"}))
        item = saved["results"][0]["id"]
        self.cli("lookup", "--token-file", token_file, "--subject", "svc")
        self.cli("lookup", "--token-file", token_file, "--query", "svc", "--explain", "--limit", "3")
        self.cli("bundle", "--token-file", token_file, "--budget-tokens", "400")
        self.cli("get", "--token-file", token_file, "--item-id", item, "--rev", "1")
        self.assertTrue(self.cli("history", "--token-file", token_file, "--item-id", item)["results"][0]["current"])
        self.cli("pin", "--token-file", token_file, "--item-id", item)
        self.cli("unpin", "--token-file", token_file, "--item-id", item)
        self.cli("retract", "--token-file", token_file, "--item-id", item, "--rev", "1", "--request-id", "f2")
        body = self.dir / "body.md"
        body.write_text("# Brief\n\nfrom a file", encoding="utf-8")
        self.cli("save", "--token-file", token_file, "--kind", "note", "--request-id", "n1", "--key", "k",
                 "--body-file", body)
        self.cli("forget", "--token-file", token_file, "--item-id", item, "--request-id", "f3")
        # ok: false -> exit 1
        self.assertEqual(self.cli("get", "--token-file", token_file, "--item-id", item,
                                  expect=1)["error"]["code"], "not_found")
        self.assertEqual(self.cli("bundle", "--token-file", token_file, "--budget-tokens", "5",
                                  expect=1)["error"]["code"], "invalid_input")
        # usage errors -> exit 2 with an invalid_input envelope
        for argv in (["lookup", "--limit", "x"], ["frobnicate"], ["review", "approve", "I"], [],
                     ["save", "--fact", "{not json"]):
            envelope = self.cli(*argv, expect=2)
            self.assertEqual((envelope["op"], envelope["error"]["code"]), ("usage", "invalid_input"))

    def test_token_file_rules(self):
        token_file = self.setup_alice()
        self.assertEqual(self.cli("lookup", "--query", "x", expect=1)["error"]["code"], "unauthorized")
        self.assertEqual(self.cli("lookup", "--query", "x", "--token-file", self.dir / "none", expect=1)
                         ["error"]["code"], "unauthorized")
        wrong = write_token(self.dir / "wrong.token", "A" * 43)
        self.assertEqual(self.cli("lookup", "--query", "x", "--token-file", wrong, expect=1)["error"]["code"],
                         "unauthorized")
        os.chmod(token_file, 0o644)
        self.assertEqual(self.cli("lookup", "--query", "x", "--token-file", token_file, expect=1)["error"]["code"],
                         "unsafe_path")
        os.chmod(token_file, 0o600)
        with mock.patch.dict(os.environ, {cli.TOKEN_ENV: str(token_file)}):
            self.cli("lookup", "--query", "x")

    def test_store_errors_are_envelopes(self):
        token_file = self.setup_alice()
        self.assertEqual(self.cli("doctor", "--store", self.dir / "absent.sqlite3", store=False, expect=1)
                         ["error"]["code"], "wrong_store")
        os.chmod(self.path, 0o644)
        self.assertEqual(self.cli("lookup", "--query", "x", "--token-file", token_file, expect=1)
                         ["error"]["code"], "unsafe_path")
        os.chmod(self.path, 0o600)

    def test_unexpected_errors_become_internal(self):
        token_file = self.setup_alice()
        with mock.patch("recall_v2.api.client.Client.lookup", side_effect=RuntimeError("boom secret")):
            envelope = self.cli("lookup", "--query", "x", "--token-file", token_file, expect=1)
        self.assertEqual(envelope["error"]["code"], "internal")
        self.assertNotIn("secret", json.dumps(envelope))
        with mock.patch("recall_v2.api.operator.Operator.doctor", side_effect=RuntimeError("boom")):
            self.assertEqual(self.cli("doctor", expect=1)["error"]["code"], "internal")


class ServerCommandTests(CliCase):
    def test_mcp_and_serve_without_the_extra_report_missing_dependency(self):
        token_file = self.setup_alice()
        with mock.patch.dict(sys.modules, {"mcp": None}):
            self.assertEqual(self.cli("serve", expect=1)["error"]["code"], "missing_dependency")
            with mock.patch.dict(os.environ, {cli.TOKEN_ENV: str(token_file)}):
                envelope = self.cli("mcp", expect=1)
        self.assertEqual(envelope["error"]["code"], "missing_dependency")
        self.assertEqual(envelope["error"]["details"], {"extra": "v2"})

    def test_mcp_without_a_token_file_is_unauthorized(self):
        self.setup_alice()
        with mock.patch.dict(os.environ, {cli.TOKEN_ENV: ""}):
            self.assertEqual(self.cli("mcp", expect=1)["error"]["code"], "unauthorized")


class ModuleEntryPointTests(CliCase):
    def test_python_dash_m_recall_v2(self):
        token_file = self.setup_alice()
        env = {"PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ.get("PATH", "")}

        def run(*argv):
            return subprocess.run([sys.executable, "-m", "recall_v2", *map(str, argv), "--store", str(self.path)],
                                  capture_output=True, text=True, env=env, cwd=str(self.dir), timeout=60)
        saved = run("save", "--token-file", token_file, "--kind", "note", "--request-id", "r", "--key", "k",
                    "--body", "hello from a subprocess")
        self.assertEqual(saved.returncode, 0, saved.stderr)
        found = run("lookup", "--token-file", token_file, "--query", "subprocess")
        self.assertEqual(json.loads(found.stdout)["count"], 1)
        self.assertEqual(found.stderr, "")
        self.assertEqual(run("bogus").returncode, 2)
        self.assertEqual(run("get", "--token-file", token_file, "--item-id", "x").returncode, 1)


if __name__ == "__main__":
    unittest.main()
