"""Kinds, scopes, revision states, save rules and identifier encoding."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Optional

from . import normalise, timefmt
from .errors import invalid

KINDS = ("note", "fact", "checkpoint", "brief", "excerpt")
SCOPES = ("private", "project")
STATES = ("candidate", "accepted", "rejected")
OPERATOR_ACTOR = "~operator"

KIB = 1024
TITLE_MAX_BYTES = 256
MAX_REV = 9_999_999
RECEIPT_TTL_MICROS = 30 * timefmt.MICROS_PER_DAY

# kind -> (allowed scopes, body limit in bytes, expected_revision required)
SAVE_RULES = {
    "note": (("private", "project"), 4 * KIB, False),
    "fact": (("private", "project"), 2 * KIB, False),
    "checkpoint": (("private",), 8 * KIB, True),
    "brief": (("project",), 48 * KIB, True),
    "excerpt": (("private", "project"), 2 * KIB, False),
}
SOURCE_REF_MAX_BYTES = 1 * KIB

_ALLOWED_TRANSITIONS = {("candidate", "accepted"), ("candidate", "rejected")}


def check_transition(old: str, new: str) -> None:
    """Only candidate -> accepted and candidate -> rejected are allowed."""
    if (old, new) not in _ALLOWED_TRANSITIONS:
        raise invalid(f"A {old} revision cannot become {new}.")


def state_on_write(scope: str) -> str:
    """Private-scope saves are accepted; project-scope saves are candidates."""
    return "accepted" if scope == "private" else "candidate"


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def encode_ulid(milliseconds: int, randomness: bytes) -> str:
    """26-character ULID from a 48-bit millisecond time and 80 bits of randomness."""
    if len(randomness) != 10 or not 0 <= milliseconds < 2 ** 48:
        raise ValueError("bad ULID inputs")
    value = (milliseconds << 80) | int.from_bytes(randomness, "big")
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


@dataclass(frozen=True)
class FactInput:
    subject: str
    predicate: str
    value: Any
    value_json: str
    key: str


@dataclass(frozen=True)
class SaveRequest:
    kind: str
    scope: str
    project: Optional[str]
    key: str
    title: Optional[str]
    body: Optional[str]
    fact: Optional[FactInput]
    valid_from: Optional[int]
    valid_to: Optional[int]
    expected_revision: Optional[int]
    source_ref: Optional[str]

    def fingerprint_payload(self) -> dict:
        return {
            "op": "save", "kind": self.kind, "scope": self.scope, "project": self.project, "key": self.key,
            "title": self.title, "body": self.body,
            "fact": None if self.fact is None else [self.fact.subject, self.fact.predicate, self.fact.value_json],
            "valid_from": self.valid_from, "valid_to": self.valid_to,
            "expected_revision": self.expected_revision, "source_ref": self.source_ref,
        }


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _text(value, what: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise invalid(f"{what} must be text.")
    return value


def validate_save(*, kind, scope="private", project=None, key=None, title=None, body=None, fact=None,
                  valid_from=None, valid_to=None, expected_revision=None, source_ref=None) -> SaveRequest:
    """Apply the per-kind save rules (section 3.1). Raises invalid_input."""
    if kind not in SAVE_RULES:
        raise invalid("Unknown kind.")
    scopes, limit, needs_expected = SAVE_RULES[kind]
    if scope not in SCOPES:
        raise invalid("scope must be private or project.")
    if scope not in scopes:
        raise invalid(f"A {kind} cannot be saved in {scope} scope.")
    if scope == "project":
        normalise.check_id(project, normalise.PROJECT_ID, "project")
    elif project is not None:
        raise invalid("project is only allowed with project scope.")
    if expected_revision is not None and (isinstance(expected_revision, bool)
                                          or not isinstance(expected_revision, int)
                                          or not 0 <= expected_revision <= MAX_REV):
        raise invalid("expected_revision must be a non-negative integer.")
    if needs_expected and expected_revision is None:
        raise invalid(f"A {kind} save requires expected_revision.")
    title = _text(title, "title")
    if title is not None and _utf8_len(title) > TITLE_MAX_BYTES:
        raise invalid("title exceeds 256 bytes.")
    body = _text(body, "body")
    source_ref = _text(source_ref, "source_ref")
    if kind != "excerpt" and source_ref is not None:
        raise invalid("source_ref is only allowed for excerpts.")
    valid_from = timefmt.coerce(valid_from)
    valid_to = timefmt.coerce(valid_to)
    if valid_from is not None and valid_to is not None and valid_to <= valid_from:
        raise invalid("valid_to must be later than valid_from.")

    fact_input = None
    if kind == "fact":
        if key is not None or body is not None or title is not None:
            raise invalid("Facts are addressed by subject and predicate, not key, title or body.")
        if not isinstance(fact, dict) or set(fact) != {"subject", "predicate", "value"}:
            raise invalid("fact must have exactly subject, predicate and value.")
        subject, predicate = _text(fact["subject"], "subject"), _text(fact["predicate"], "predicate")
        if subject is None or predicate is None:
            raise invalid("subject and predicate are required.")
        value_json = normalise.canonical_value(fact["value"])
        if _utf8_len(value_json) > limit:
            raise invalid("The fact value exceeds 2 KiB of canonical JSON.")
        key = normalise.fact_key(subject, predicate)
        fact_input = FactInput(subject, predicate, fact["value"], value_json, key)
    else:
        if fact is not None:
            raise invalid("fact is only allowed for kind fact.")
        normalise.check_id(key, normalise.ITEM_KEY, "key")
        if body is None or not body.strip():
            raise invalid("body is required.")
        if kind == "checkpoint":
            try:
                parsed = json.loads(body)
            except ValueError:
                raise invalid("A checkpoint body must be a JSON object.") from None
            if not isinstance(parsed, dict):
                raise invalid("A checkpoint body must be a JSON object.")
            body = normalise.canonical_json(parsed)
        if _utf8_len(body) > limit:
            raise invalid(f"The {kind} body exceeds its limit.")
        if kind == "excerpt":
            if source_ref is None or not source_ref.startswith("https://") or _utf8_len(source_ref) > SOURCE_REF_MAX_BYTES:
                raise invalid("An excerpt needs an HTTPS source_ref of at most 1 KiB.")
    return SaveRequest(kind, scope, project, key, title, body, fact_input, valid_from, valid_to,
                       expected_revision, source_ref)
