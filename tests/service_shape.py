"""Records the MCP wire shape (canonical JSON text) of each tool result for a fixed scenario.

Used by tests/test_service_shape.py against the current code, and by the one-time generator that
produced tests/fixtures/mcp_result_shapes.json from the pre-rename code at commit 1777310.
Timestamps and proposal IDs are masked because they change on every run.
"""
from __future__ import annotations

import re

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00")
PROPOSAL_ID = re.compile(r"[0-9a-f]{32}")


def _mask(text):
    return PROPOSAL_ID.sub("<id>", TIMESTAMP.sub("<timestamp>", text))


def record(store, *, store_error, invoke, canonical_json, storage_error, invalid_input,
           credential_digest, add_action, public_ok_key="ok"):
    """Run the scenario and return a list of {operation, arguments, text} with volatile values masked."""
    store.initialize(create=True)
    alpha = store.change_credential("alpha", add_action)["token"]
    beta = store.change_credential("beta", add_action)["token"]
    store.project_add("default")
    store.change_membership("default", "alpha", grant=True)
    alpha_digest, beta_digest = credential_digest(alpha), credential_digest(beta)
    stranger_digest = credential_digest("not-a-credential")
    steps = []

    def call(operation, digest, arguments):
        try:
            value = invoke(store, operation, digest, arguments)
            result = {public_ok_key: True, **value}
        except store_error as exc:
            result = exc.result
        except (OSError,):  # the MCP adapter maps storage errors to this shape
            result = storage_error
        steps.append({"operation": operation, "arguments": arguments,
                      "text": _mask(canonical_json(result))})

    checkpoint = {"summary": "plan", "decisions": ["use sqlite"], "questions": [], "actions": ["ship"],
                  "source_refs": ["ref-1"]}
    call("checkpoint_save", alpha_digest, {"scope": "task", "key": "plan", "expected_revision": 0,
                                           "request_id": "r1", "checkpoint": checkpoint})
    call("checkpoint_get", alpha_digest, {"scope": "task", "key": "plan"})
    call("checkpoint_save", alpha_digest, {"scope": "task", "key": "plan", "expected_revision": 0,
                                           "request_id": "r2", "checkpoint": checkpoint})
    call("checkpoint_save", alpha_digest, {"scope": "task", "key": "plan", "expected_revision": 1,
                                           "request_id": "r3", "checkpoint": checkpoint})
    call("checkpoint_get", alpha_digest, {"scope": "task", "key": "plan", "revision": 1})
    call("checkpoint_get", beta_digest, {"scope": "task", "key": "plan"})
    call("checkpoint_get", stranger_digest, {"scope": "task", "key": "plan"})

    call("brief_propose", alpha_digest, {"project": "default", "key": "brief", "expected_revision": 0,
                                         "request_id": "p1", "markdown": "# Brief\n\nShared plan."})
    call("excerpt_propose", alpha_digest, {"project": "default", "key": "quote", "expected_revision": 0,
                                           "request_id": "p2", "excerpt": "exact words",
                                           "source_ref": "https://example.test/source"})
    pending = store.list_proposals("default", "pending", 20)
    by_kind = {item["kind"]: item["proposal_id"] for item in pending["proposals"]}
    steps.append({"operation": "operator_list_proposals", "arguments": {"project": "default"},
                  "text": _mask(canonical_json(pending))})
    for kind, decision in (("brief", "approve"), ("excerpt", "reject")):
        review = store.review(by_kind[kind], decision, "operator")
        steps.append({"operation": "operator_review", "arguments": {"kind": kind, "decision": decision},
                      "text": _mask(canonical_json(review))})

    call("brief_get", alpha_digest, {"project": "default", "key": "brief"})
    call("excerpt_get", alpha_digest, {"project": "default", "key": "quote"})
    call("project_search", alpha_digest, {"query": "plan"})
    call("project_search", beta_digest, {"query": "plan"})
    return steps
