"""Trusted local operator commands for the Recall service store."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys

from .store import ServiceStore, StoreError, new_private_file


IDENTIFIER = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}(?![\s\S])"


def identifier(value):
    if re.fullmatch(IDENTIFIER, value) is None:
        raise argparse.ArgumentTypeError("use 1–64 letters, digits, dots, underscores, or hyphens")
    return value


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number < 1 << 63:
        raise argparse.ArgumentTypeError("use a positive integer below 2^63")
    return number


def bounded_limit(value):
    limit = positive_integer(value)
    if limit > 100:
        raise argparse.ArgumentTypeError("use a limit of at most 100")
    return limit


def retention_days(value):
    try:
        number = int(value)
    except ValueError:
        number = -1
    if not 0 <= number <= 36500:
        raise argparse.ArgumentTypeError("use a number of days between 0 and 36500")
    return number


def add_parser(subparsers):
    _add_store_parser(subparsers, "service", "operate the isolated Recall service store")
    # Deprecated alias for one release: hidden from help, warns on stderr at run time.
    _add_store_parser(subparsers, "openbot", argparse.SUPPRESS)  # neutral-core: allow deprecated alias


def _add_store_parser(subparsers, name, help_text):
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("--db", type=Path, required=True, help="database path in a private directory")
    sub = parser.add_subparsers(dest="store_cmd", required=True)
    sub.add_parser("init", help="create or migrate the service database")
    sub.add_parser("health", help="check readiness of the database")
    serve = sub.add_parser("serve", help="serve the HTTP MCP endpoint")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    serve.add_argument("--allowed-host", action="append", default=[], help="allow an additional exact Host header")
    serve.add_argument("--allowed-origin", action="append", default=[], help="allow an additional exact Origin")
    for command in ("principal-add", "principal-rotate", "principal-revoke"):
        sub.add_parser(command, help="create, rotate, or revoke a principal credential").add_argument(
            "principal", type=identifier,
        )
    for command in ("bot-add", "bot-rotate", "bot-revoke"):  # deprecated aliases
        sub.add_parser(command, help=argparse.SUPPRESS).add_argument("principal", type=identifier)
    sub.add_parser("project-add", help="create a project with no members").add_argument("project", type=identifier)
    for command in ("member-grant", "member-revoke"):
        member = sub.add_parser(command)
        member.add_argument("project", type=identifier)
        member.add_argument("principal", type=identifier)
    proposal_list = sub.add_parser("proposal-list", help="list proposals without content")
    proposal_list.add_argument("project", type=identifier)
    proposal_list.add_argument("--status", choices=("pending", "accepted", "rejected"), default="pending")
    proposal_list.add_argument("--limit", type=bounded_limit, default=20)
    sub.add_parser("proposal-show", help="read a proposal for local review").add_argument(
        "proposal_id", type=identifier,
    )
    review = sub.add_parser("review", help="approve or reject a proposal as a trusted local operator")
    review.add_argument("proposal_id", type=identifier)
    review.add_argument("decision", choices=("approve", "reject"))
    review.add_argument("--reviewer", type=identifier, required=True, help="operator's audit label")
    export = sub.add_parser("export", help="export an accepted brief to a new Markdown file")
    export.add_argument("project", type=identifier)
    export.add_argument("key", type=identifier)
    export.add_argument("file", type=Path)
    checkpoint_delete = sub.add_parser("checkpoint-delete", help="erase a private checkpoint and its history")
    checkpoint_delete.add_argument("principal", type=identifier)
    checkpoint_delete.add_argument("scope", choices=("task", "project"))
    checkpoint_delete.add_argument("key", type=identifier)
    publication_delete = sub.add_parser("publication-delete", help="erase a publication and its proposals")
    publication_delete.add_argument("project", type=identifier)
    publication_delete.add_argument("kind", choices=("brief", "excerpt"))
    publication_delete.add_argument("key", type=identifier)
    prune = sub.add_parser("prune", help="apply age-based retention to content, retries, and audit events")
    prune.add_argument("--checkpoint-days", type=retention_days, default=90)
    prune.add_argument("--publication-days", type=retention_days, default=365)
    prune.add_argument("--proposal-days", type=retention_days, default=30)
    prune.add_argument("--audit-days", type=retention_days, default=90)
    prune.add_argument("--apply", action="store_true", required=True, help="apply the retention settings")
    for command, help_text in (
        ("audit", "list recent metadata-only audit events"),
        ("inventory", "list principal and project identifiers and credential status"),
        ("member-list", "list the current members of a project"),
    ):
        listing = sub.add_parser(command, help=help_text)
        listing.add_argument("--limit", type=bounded_limit, default=20)
        if command == "member-list":
            listing.add_argument("project", type=identifier)
    sub.add_parser("backup", help="copy the current store to a new private database").add_argument("file", type=Path)
    sub.add_parser("restore", help="restore a backup into the new --db path").add_argument("file", type=Path)


def run(args):
    if getattr(args, "cmd", None) == "openbot":  # neutral-core: allow deprecated alias
        print('warning: "recall openbot" is deprecated; use "recall service".', file=sys.stderr)  # neutral-core: allow deprecated alias
    store = ServiceStore(args.db)
    command = {"bot-add": "principal-add", "bot-rotate": "principal-rotate", "bot-revoke": "principal-revoke"}.get(
        args.store_cmd, args.store_cmd,
    )
    try:
        if command == "init":
            result = store.initialize(create=True)
        elif command == "health":
            result = store.health()
        elif command == "serve":
            try:
                from .mcp import serve
            except ModuleNotFoundError:
                raise StoreError("missing_dependency", 'Install Recall with the "service" extra.') from None
            if not 1 <= args.port <= 65535:
                raise StoreError("invalid_input", "Port must be in 1–65535.")
            store.initialize()
            serve(store, args.host, args.port, args.allowed_host, args.allowed_origin)
            return 0
        elif command in ("principal-add", "principal-rotate", "principal-revoke"):
            result = store.change_credential(args.principal, command)
        elif command == "project-add":
            result = store.project_add(args.project)
        elif command in ("member-grant", "member-revoke"):
            result = store.change_membership(args.project, args.principal, grant=command == "member-grant")
        elif command == "proposal-list":
            result = store.list_proposals(args.project, args.status, args.limit)
        elif command == "proposal-show":
            result = store.show_proposal(args.proposal_id)
        elif command == "review":
            result = store.review(args.proposal_id, args.decision, args.reviewer)
        elif command == "export":
            markdown = store.export_brief(args.project, args.key)
            with new_private_file(args.file) as staged:
                staged.write_bytes(markdown.encode("utf-8"))
            result = {"exported": True}
        elif command == "checkpoint-delete":
            result = store.delete_checkpoint(args.principal, args.scope, args.key)
        elif command == "publication-delete":
            result = store.delete_publication(args.project, args.kind, args.key)
        elif command == "prune":
            result = store.prune(args.checkpoint_days, args.publication_days, args.proposal_days, args.audit_days)
        elif command == "audit":
            result = store.audit_events(args.limit)
        elif command == "inventory":
            result = store.inventory(args.limit)
        elif command == "member-list":
            result = store.member_list(args.project, args.limit)
        elif command == "backup":
            result = store.copy_to(ServiceStore(args.file), action="backup")
        elif command == "restore":
            result = ServiceStore(args.file).copy_to(store, action="restore")
    except StoreError as exc:
        print(json.dumps(exc.result), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print(json.dumps({"ok": False, "error": "storage_unavailable", "message": "Cannot access the store or file."}),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
