"""Official Streamable HTTP adapter for the isolated OpenBot context service."""
from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import re
import sqlite3
from urllib.parse import urlsplit

import anyio
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from .context_policy import validate_context
from .openbot_cli import IDENTIFIER
from .openbot_store import StoreError, canonical_json, credential_digest


ID = {"type": "string", "pattern": IDENTIFIER}
SCOPE = {"type": "string", "enum": ["task", "project"]}
REVISION = {"type": "integer", "minimum": 0, "maximum": (1 << 53) - 1}
TOKEN = re.compile(r"(?i:Bearer) ([A-Za-z0-9_-]{43})")
STORAGE_ERROR = {"ok": False, "error": "storage_unavailable", "message": "The context store is unavailable."}
INVALID_INPUT = {"ok": False, "error": "invalid_input", "message": "Arguments must match the tool schema and size limits."}


def string(size):
    return {"type": "string", "minLength": 1, "maxLength": size}


def object_schema(fields, *, optional=()):
    return {
        "type": "object", "properties": fields,
        "required": [field for field in fields if field not in optional],
        "additionalProperties": False,
    }


def list_schema(size, count=12):
    return {"type": "array", "maxItems": count, "items": string(size)}


CHECKPOINT = object_schema({
    "summary": string(2000),
    "decisions": list_schema(512),
    "questions": list_schema(512),
    "actions": list_schema(512),
    "source_refs": list_schema(1024),
})
TOOL_SPECS = {
    "checkpoint_get": (
        "Read the current private checkpoint or a retained revision for this bot.",
        object_schema({"scope": SCOPE, "key": ID, "revision": {**REVISION, "minimum": 1}}, optional=("revision",)),
    ),
    "checkpoint_save": (
        "Replace this bot's private checkpoint with the expected revision and a stable request ID.",
        object_schema({"scope": SCOPE, "key": ID, "expected_revision": REVISION, "request_id": ID,
                       "checkpoint": CHECKPOINT}),
    ),
}
PUBLICATION_FIELDS = {"project": ID, "key": ID}
PROPOSAL_FIELDS = {**PUBLICATION_FIELDS, "expected_revision": REVISION, "request_id": ID}
TOOL_SPECS.update({
    "brief_get": ("Read the current accepted project brief, including Markdown and review provenance.",
                  object_schema(PUBLICATION_FIELDS)),
    "excerpt_get": ("Read the current accepted excerpt and its explicit source reference.",
                    object_schema(PUBLICATION_FIELDS)),
    "brief_propose": ("Submit structured Recall Markdown for operator review. This does not publish it.",
                      object_schema({**PROPOSAL_FIELDS, "markdown": string(48 * 1024)})),
    "excerpt_propose": ("Submit a short verbatim excerpt and its HTTPS source for separate operator review.",
                        object_schema({**PROPOSAL_FIELDS, "excerpt": string(2048), "source_ref": string(1024)})),
    "project_search": (
        "Find this bot's current checkpoints and accepted items in its projects. Results contain references only.",
        object_schema({"query": string(128), "project": ID, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                      optional=("project", "limit")),
    ),
})


def request_digest(headers):
    values = headers.getlist("authorization")
    match = TOKEN.fullmatch(values[0]) if len(values) == 1 else None
    return credential_digest(match[1]) if match else None


def valid_content(operation, arguments):
    if any(field in arguments and type(arguments[field]) is not int for field in ("expected_revision", "revision", "limit")):
        return False
    try:
        if operation == "checkpoint_save":
            content = arguments["checkpoint"]
            if len(canonical_json(content).encode("utf-8")) > 8192:
                return False
            return all(entry.strip() for value in content.values()
                       for entry in (value if isinstance(value, list) else [value]))
        if operation == "brief_propose":
            markdown = arguments["markdown"]
            if len(markdown.encode("utf-8")) > 48 * 1024 or "\0" in markdown:
                return False
            validate_context(markdown, require_structured=True)
        if operation == "excerpt_propose":
            excerpt, source = arguments["excerpt"], arguments["source_ref"]
            if (not excerpt.strip() or "\0" in excerpt or len(excerpt.encode("utf-8")) > 2048
                    or len(source.encode("utf-8")) > 1024
                    or any(char.isspace() or ord(char) < 32 or char in "\\\x7f" for char in source)):
                return False
            link = urlsplit(source)
            return link.scheme == "https" and bool(link.hostname) and link.username is None and link.password is None
        if operation == "project_search":
            query = arguments["query"]
            return bool(query.strip()) and "\0" not in query and len(query.encode("utf-8")) <= 256
    except ValueError:
        return False
    return True


def invoke(store, operation, digest, arguments):
    kind, _, action = operation.partition("_")
    if kind == "checkpoint" or operation == "project_search":
        return getattr(store, operation)(digest, **arguments)
    if action == "get":
        return store.publication_get(digest, kind, **arguments)
    fields = ("markdown",) if kind == "brief" else ("excerpt", "source_ref")
    content = {field: arguments[field] for field in fields}
    metadata = {field: value for field, value in arguments.items() if field not in fields}
    return store.publication_propose(digest, kind, content=content, **metadata)


def create_app(store, hosts, origins):
    server = Server("recall-openbot")
    validators = {name: Draft202012Validator(schema) for name, (_, schema) in TOOL_SPECS.items()}
    tools = [
        types.Tool(name=f"recall_{name}", description=description, inputSchema=schema)
        for name, (description, schema) in TOOL_SPECS.items()
    ]

    @server.list_tools()
    async def list_tools():
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        operation = name.removeprefix("recall_")
        validator = validators.get(operation) if name.startswith("recall_") else None
        failure = True
        if not validator or not validator.is_valid(arguments) or not valid_content(operation, arguments):
            result = INVALID_INPUT
        else:
            digest = request_digest(server.request_context.request.headers)
            try:
                value = await anyio.to_thread.run_sync(invoke, store, operation, digest, arguments)
                result = {"ok": True, **value}
                failure = False
            except StoreError as exc:
                result = exc.result
            except (OSError, sqlite3.Error):
                result = STORAGE_ERROR
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=canonical_json(result))],
            structuredContent=result, isError=failure,
        )

    manager = StreamableHTTPSessionManager(
        server, stateless=True, json_response=True, max_request_body_size=128 * 1024,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins,
        ),
    )

    async def mcp_endpoint(scope, receive, send):
        digest = request_digest(Headers(scope=scope))
        try:
            if digest is None:
                raise StoreError("unauthorized", "A current bot credential is required.")
            await anyio.to_thread.run_sync(store.authorize, digest)
        except StoreError as exc:
            status = 401 if exc.result["error"] == "unauthorized" else 503
            headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
            await JSONResponse(exc.result, status_code=status, headers=headers)(scope, receive, send)
        except (OSError, sqlite3.Error):
            await JSONResponse(STORAGE_ERROR, status_code=503)(scope, receive, send)
        else:
            await manager.handle_request(scope, receive, send)

    class McpEndpoint:
        async def __call__(self, scope, receive, send):
            async def send_uncached(message):
                if message["type"] == "http.response.start":
                    MutableHeaders(scope=message)["Cache-Control"] = "no-store"
                await send(message)

            await mcp_endpoint(scope, receive, send_uncached)

    async def health(_request):
        try:
            state = await anyio.to_thread.run_sync(store.health)
        except (OSError, sqlite3.Error, StoreError):
            return JSONResponse({"status": "unavailable"}, status_code=503, headers={"Cache-Control": "no-store"})
        return JSONResponse(state, headers={"Cache-Control": "no-store"})

    @asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    return Starlette(routes=[Route("/mcp", McpEndpoint()), Route("/health", health)], lifespan=lifespan)


def serve(store, host, port, allowed_hosts, allowed_origins):
    address = f"[{host}]" if host == "::1" else host
    hosts = [address, f"{address}:{port}", "localhost", f"localhost:{port}", *allowed_hosts]
    if any("*" in item or not item.isascii() or any(char.isspace() for char in item)
           for item in (*hosts, *allowed_origins)):
        raise StoreError("invalid_input", "Supply exact host and Origin allowlist entries.")
    # The SDK can log raw invalid headers, tool names, and parse errors.
    sdk_logger = logging.getLogger("mcp")
    sdk_logger.addHandler(logging.NullHandler())
    sdk_logger.propagate = False
    app = create_app(store, hosts, allowed_origins)
    uvicorn.run(app, host=host, port=port, access_log=False, proxy_headers=False, log_level="warning")
