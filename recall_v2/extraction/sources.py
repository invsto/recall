"""The source registry: a source, its adapter config and its extraction settings (section 3.2).

Defaults: extraction off; no endpoint; cloud not allowed; redaction rules secrets, emails and
private_tags on; 50,000 input tokens per run and 200,000 per source per day; min_confidence 0.5;
allowed categories = every category the adapter describes. A target scope is required: a principal's
private scope, or a project with the principal (a member) that owns the candidates written there.

Endpoints are strings: `local:<name>` (an in-process model), `command:<name>` (a local command), or an
http(s) URL. Local means `local:`, `command:`, or a URL whose host is loopback. Only the operator may
manage sources.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import ipaddress
import json
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from ..adapters import ADAPTERS, SourceAdapter, get_adapter
from ..core import normalise, timefmt
from ..core.errors import RecallError, invalid
from ..governance.authority import require_operator
from ..store import audit
from ..store.connection import read_transaction, write_transaction
from . import redaction

OPERATOR = "~operator"
DEFAULT_RUN_BUDGET = 50_000
DEFAULT_DAY_BUDGET = 200_000
DEFAULT_MIN_CONFIDENCE = 0.5
MAX_BUDGET = 100_000_000
ENDPOINT_MAX_BYTES = 1024
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}")
UNSET = object()


def _model_id_ok(value) -> bool:
    return isinstance(value, str) and MODEL_ID.fullmatch(value) is not None


def endpoint_is_local(endpoint: str) -> bool:
    """`local:` and `command:` endpoints are local; an http(s) URL is local only on a loopback host."""
    if endpoint.startswith(("local:", "command:")):
        return True
    host = urlsplit(endpoint).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_endpoint(endpoint) -> Optional[str]:
    if endpoint is None:
        return None
    if not isinstance(endpoint, str) or not endpoint or len(endpoint.encode("utf-8")) > ENDPOINT_MAX_BYTES \
            or any(c.isspace() for c in endpoint):
        raise invalid("endpoint must be a non-empty string without whitespace.")
    if endpoint.startswith(("local:", "command:")):
        if len(endpoint.split(":", 1)[1]) == 0:
            raise invalid("A local endpoint needs a name.")
        return endpoint
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise invalid("endpoint must be local:<name>, command:<name> or an http(s) URL without credentials.")
    return endpoint


@dataclass(frozen=True)
class SourceSettings:
    source_id: str
    adapter: str
    uri: Optional[str]
    status: str
    enabled: bool
    scope_kind: str
    owner_id: str
    project_id: Optional[str]
    endpoint: Optional[str]
    model_id: Optional[str]
    cloud_allowed: bool
    categories: tuple
    redaction: dict
    run_token_budget: int
    day_token_budget: int
    min_confidence: float
    adapter_config: dict

    @property
    def local(self) -> bool:
        return self.endpoint is not None and endpoint_is_local(self.endpoint)

    def target(self) -> dict:
        if self.scope_kind == "private":
            return {"scope": "private", "id": self.owner_id}
        return {"scope": "project", "id": self.project_id, "owner": self.owner_id}

    def make_adapter(self) -> SourceAdapter:
        return get_adapter(self.adapter, self.adapter_config)

    def public(self) -> dict:
        """Settings as the operator sees them (custom redaction patterns are shown by name only)."""
        return {
            "source_id": self.source_id, "adapter": self.adapter, "uri": self.uri, "status": self.status,
            "enabled": self.enabled, "target": self.target(), "endpoint": self.endpoint, "model_id": self.model_id,
            "local": self.local, "cloud_allowed": self.cloud_allowed, "categories": list(self.categories),
            "redaction": [n for n in redaction.BUILTIN_RULES if self.redaction[n]]
                         + [r["name"] for r in self.redaction["custom"]],
            "run_token_budget": self.run_token_budget, "day_token_budget": self.day_token_budget,
            "min_confidence": self.min_confidence,
        }


def load(db, source_id: str) -> SourceSettings:
    row = db.execute(
        "SELECT s.id, s.adapter, s.uri, s.status, c.* FROM sources AS s JOIN source_settings AS c ON c.source_id = s.id"
        " WHERE s.id = ?", (source_id,)).fetchone()
    if row is None:
        raise RecallError("not_found", "No such source.")
    return SourceSettings(
        source_id=row["id"], adapter=row["adapter"], uri=row["uri"], status=row["status"],
        enabled=bool(row["enabled"]), scope_kind=row["scope_kind"], owner_id=row["owner_id"],
        project_id=row["project_id"], endpoint=row["endpoint"], model_id=row["model_id"],
        cloud_allowed=bool(row["cloud_allowed"]), categories=tuple(json.loads(row["categories_json"])),
        redaction=json.loads(row["redaction_json"]), run_token_budget=row["run_token_budget"],
        day_token_budget=row["day_token_budget"], min_confidence=row["min_confidence"],
        adapter_config=json.loads(row["adapter_config_json"]))


# -- validation ------------------------------------------------------------------------------------

def _target(db, target) -> tuple[str, str, Optional[str]]:
    if not isinstance(target, Mapping):
        raise invalid("target must be an object.")
    scope = target.get("scope")
    if scope == "private":
        if set(target) != {"scope", "principal"}:
            raise invalid("A private target has exactly scope and principal.")
        project = None
    elif scope == "project":
        if set(target) != {"scope", "project", "principal"}:
            raise invalid("A project target has exactly scope, project and principal.")
        project = normalise.check_id(target["project"], normalise.PROJECT_ID, "project")
    else:
        raise invalid("target.scope must be private or project.")
    owner = normalise.check_id(target["principal"], normalise.PRINCIPAL_ID, "principal")
    row = db.execute("SELECT revoked_at FROM principals WHERE id = ?", (owner,)).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise RecallError("not_found", "No such principal.")
    if project is not None and db.execute("SELECT 1 FROM memberships WHERE principal_id = ? AND project_id = ?",
                                          (owner, project)).fetchone() is None:
        raise RecallError("not_found", "No such project membership.")
    return scope, owner, project


def _budget(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_BUDGET:
        raise invalid(f"{what} must be a positive integer.")
    return value


def _confidence(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise invalid("min_confidence must be a number from 0 to 1.")
    return float(value)


def _categories(value, adapter: SourceAdapter) -> list[str]:
    described = list(adapter.describe()["categories"])
    if value is None:
        return described
    if not isinstance(value, (list, tuple)) or not value or not all(isinstance(c, str) for c in value):
        raise invalid("categories must be a non-empty list of names.")
    unknown = sorted(set(value) - set(described))
    if unknown:
        raise invalid("Unknown category for this adapter.", categories=unknown)
    return sorted(set(value))


def _adapter(name, config) -> tuple[SourceAdapter, dict]:
    if name not in ADAPTERS:
        raise invalid("Unknown adapter.", adapter=name)
    if not isinstance(config, Mapping) or not isinstance(config.get("path"), str) or not config["path"]:
        raise invalid("The adapter config needs a path.")
    try:
        json.dumps(dict(config))
        adapter = get_adapter(name, dict(config))
    except (TypeError, ValueError) as exc:
        raise invalid("Invalid adapter config.", reason=str(exc)) from None
    return adapter, dict(config)


def content_hash(adapter: SourceAdapter) -> tuple[bytes, list]:
    """SHA-256 over every discovered ref's URI and content hash, in discovery order."""
    refs = list(adapter.discover())
    digest = sha256()
    for ref in refs:
        digest.update(ref.uri.encode("utf-8") + b"\x00" + ref.content_hash)
    return digest.digest(), refs


# -- operator functions ----------------------------------------------------------------------------

def add(session, *, adapter: str, config: Mapping[str, Any], target: Mapping[str, Any], endpoint=None,
        model_id=None, cloud_allowed: bool = False, categories=None, redaction_rules=None,
        run_token_budget: int = DEFAULT_RUN_BUDGET, day_token_budget: int = DEFAULT_DAY_BUDGET,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> dict:
    """Register a source. Extraction starts disabled; enable it with `update(..., enabled=True)`."""
    store = require_operator(session).store
    instance, config = _adapter(adapter, config)
    endpoint = check_endpoint(endpoint)
    if model_id is not None and not _model_id_ok(model_id):
        raise invalid("model_id must be 1 to 128 characters of [A-Za-z0-9_.:/@+-].")
    if not isinstance(cloud_allowed, bool):
        raise invalid("cloud_allowed must be a boolean.")
    cats = _categories(categories, instance)
    rules = redaction.normalise_settings(redaction_rules)
    run_budget = _budget(run_token_budget, "run_token_budget")
    day_budget = _budget(day_token_budget, "day_token_budget")
    floor = _confidence(min_confidence)
    digest, refs = content_hash(instance)
    with write_transaction(store.db) as db:
        now = store.clock()
        scope, owner, project = _target(db, target)
        source_id = store._ulid(now)
        db.execute("INSERT INTO sources (id, adapter, uri, content_hash, captured_at, status) VALUES (?, ?, ?, ?, ?, ?)",
                   (source_id, adapter, config["path"], digest, now, "live" if refs else "missing"))
        db.execute(
            "INSERT INTO source_settings (source_id, enabled, scope_kind, owner_id, project_id, endpoint, model_id,"
            " cloud_allowed, categories_json, redaction_json, run_token_budget, day_token_budget, min_confidence,"
            " adapter_config_json, updated_at) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, scope, owner, project, endpoint, model_id, int(cloud_allowed), normalise.canonical_json(cats),
             normalise.canonical_json(rules), run_budget, day_budget, floor, normalise.canonical_json(config), now))
        audit.append(db, at=now, actor=OPERATOR, action="source_add", project_id=project)
        return load(db, source_id).public()


def update(session, source_id: str, *, enabled=UNSET, target=UNSET, endpoint=UNSET, model_id=UNSET,
           cloud_allowed=UNSET, categories=UNSET, redaction_rules=UNSET, run_token_budget=UNSET,
           day_token_budget=UNSET, min_confidence=UNSET) -> dict:
    """Change extraction settings. Any change changes the next manifest's hash."""
    store = require_operator(session).store
    with write_transaction(store.db) as db:
        now = store.clock()
        current = load(db, source_id)
        changes: dict[str, Any] = {}
        if enabled is not UNSET:
            if not isinstance(enabled, bool):
                raise invalid("enabled must be a boolean.")
            changes["enabled"] = int(enabled)
        if target is not UNSET:
            changes["scope_kind"], changes["owner_id"], changes["project_id"] = _target(db, target)
        if endpoint is not UNSET:
            changes["endpoint"] = check_endpoint(endpoint)
        if model_id is not UNSET:
            if model_id is not None and not _model_id_ok(model_id):
                raise invalid("model_id must be 1 to 128 characters of [A-Za-z0-9_.:/@+-].")
            changes["model_id"] = model_id
        if cloud_allowed is not UNSET:
            if not isinstance(cloud_allowed, bool):
                raise invalid("cloud_allowed must be a boolean.")
            changes["cloud_allowed"] = int(cloud_allowed)
        if categories is not UNSET:
            changes["categories_json"] = normalise.canonical_json(_categories(categories, current.make_adapter()))
        if redaction_rules is not UNSET:
            changes["redaction_json"] = normalise.canonical_json(redaction.normalise_settings(redaction_rules))
        if run_token_budget is not UNSET:
            changes["run_token_budget"] = _budget(run_token_budget, "run_token_budget")
        if day_token_budget is not UNSET:
            changes["day_token_budget"] = _budget(day_token_budget, "day_token_budget")
        if min_confidence is not UNSET:
            changes["min_confidence"] = _confidence(min_confidence)
        if changes:
            changes["updated_at"] = now
            assignments = ", ".join(f"{column} = :{column}" for column in changes)
            db.execute(f"UPDATE source_settings SET {assignments} WHERE source_id = :source_id",
                       {**changes, "source_id": source_id})
            updated = load(db, source_id)
            audit.append(db, at=now, actor=OPERATOR, action="source_update", project_id=updated.project_id)
        return load(db, source_id).public()


def list_sources(session) -> list[dict]:
    store = require_operator(session).store
    with read_transaction(store.db) as db:
        return [load(db, row[0]).public() for row in db.execute("SELECT source_id FROM source_settings ORDER BY source_id")]


def day_start(now: int) -> int:
    """Start of the UTC calendar day containing `now` (the per-day budget window)."""
    return now - now % timefmt.MICROS_PER_DAY
