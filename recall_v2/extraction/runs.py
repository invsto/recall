"""Extraction runs: plan (disclosure manifest), acknowledge, run, cancel, list (section 3.2).

Order of a run, each step refusing before any model call:
1. the source exists and extraction is enabled for it (`forbidden` otherwise);
2. the endpoint is local, or the source is cloud-allowed (`disclosure_required` otherwise);
3. the model client's endpoint and model ID equal the configured ones (`disclosure_required`);
4. the manifest is rebuilt from current configuration and content; its SHA-256 must equal `ack`, and a
   manifest with that hash must have been planned for this source (`disclosure_required` otherwise).
   The acknowledgement is stored with its time.
Then, chunk by chunk (each chunk already redacted): stop if the run was cancelled; stop before a call
that would exceed the per-run or per-day token budget (status `budget_stopped`); call the model outside
any transaction; write its candidates, provenance, the chunk record, the tokens used and one audit event
in one IMMEDIATE transaction. Output is candidates only, never accepted, whatever the target scope.

Audit events are content-free: actor `~operator` for plan, acknowledgement and cancel; actor
`~extract:<run_id>` for run start, each chunk, each candidate save and run end.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Optional

from ..core import model as core_model
from ..core import normalise, timefmt
from ..core.errors import RecallError, invalid
from ..governance.authority import require_operator
from ..store import audit
from ..store.connection import read_transaction, write_transaction
from . import chunking, redaction, sources
from .models import ExtractionModel

MANIFEST_VERSION = 1
OUTPUT_FIELDS = {"kind", "subject", "predicate", "value", "body", "valid_from", "confidence"}


def actor_for(run_id: str) -> str:
    return f"~extract:{run_id}"


# -- manifest --------------------------------------------------------------------------------------

def _settings_hash(settings: dict) -> str:
    return sha256(normalise.canonical_json(settings).encode("utf-8")).hexdigest()


def _done_chunks(db, model_id: str) -> set[bytes]:
    return {row[0] for row in db.execute(
        "SELECT chunk_hash FROM extraction_chunks WHERE model_id = ? AND template_version = ? AND status = 'done'",
        (model_id, chunking.TEMPLATE_VERSION))}


def _build(db, cfg: sources.SourceSettings) -> tuple[dict, list]:
    """The manifest for the source as it is now, and the chunks still to process."""
    if cfg.endpoint is None or cfg.model_id is None:
        raise invalid("Configure an endpoint and model_id before planning.")
    adapter = cfg.make_adapter()
    source_hash, refs = sources.content_hash(adapter)
    redactor = redaction.Redactor(cfg.redaction)
    plan = chunking.build_chunks(adapter, refs, cfg.categories, redactor)
    done = _done_chunks(db, cfg.model_id)
    pending = [c for c in plan.chunks if c.hash not in done]
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "source_id": cfg.source_id,
        "adapter": cfg.adapter,
        "source_content_hash": source_hash.hex(),
        "refs": plan.refs,
        "target": cfg.target(),
        "endpoint": cfg.endpoint,
        "model_id": cfg.model_id,
        "local": cfg.local,
        "cloud_allowed": cfg.cloud_allowed,
        "prompt_template_version": chunking.TEMPLATE_VERSION,
        "estimator": chunking.ESTIMATOR,
        "categories": list(cfg.categories),
        "redaction": {"rules": redactor.rule_names, "settings_hash": _settings_hash(redactor.settings),
                      "counts": plan.redaction_counts},
        "records": {"read": plan.records_read, "included": plan.records_included},
        "chunk_count": len(plan.chunks),
        "chunk_hashes": [c.hash.hex() for c in plan.chunks],
        "pending_chunk_count": len(pending),
        "estimated_tokens": sum(c.tokens for c in pending),
        "budgets": {"run_tokens": cfg.run_token_budget, "day_tokens": cfg.day_token_budget},
        "min_confidence": cfg.min_confidence,
    }
    return manifest, pending


def manifest_hash(manifest: dict) -> bytes:
    return sha256(normalise.canonical_json(manifest).encode("utf-8")).digest()


def _check_privacy(cfg: sources.SourceSettings) -> None:
    if cfg.endpoint is not None and not cfg.local and not cfg.cloud_allowed:
        raise RecallError("disclosure_required",
                          "A non-local endpoint needs cloud_allowed on the source.", source_id=cfg.source_id)


def plan_run(session, source_id: str) -> dict:
    """Build, store and return the disclosure manifest: {"manifest", "manifest_hash"}. No model call."""
    store = require_operator(session).store
    with read_transaction(store.db) as db:
        cfg = sources.load(db, source_id)
        _check_privacy(cfg)
        manifest, _ = _build(db, cfg)
    digest = manifest_hash(manifest)
    with write_transaction(store.db) as db:
        now = store.clock()
        db.execute("INSERT OR IGNORE INTO extraction_manifests (manifest_hash, source_id, manifest_json, planned_at)"
                   " VALUES (?, ?, ?, ?)", (digest, source_id, normalise.canonical_json(manifest), now))
        db.execute("UPDATE sources SET content_hash = ?, status = ? WHERE id = ? AND status <> 'deleted'",
                   (bytes.fromhex(manifest["source_content_hash"]), "live" if manifest["refs"] else "missing",
                    source_id))
        audit.append(db, at=now, actor=sources.OPERATOR, action="extract_plan", project_id=cfg.project_id)
    return {"manifest": manifest, "manifest_hash": digest.hex()}


def _ack_bytes(ack) -> bytes:
    if not isinstance(ack, str) or len(ack) != 64:
        raise RecallError("disclosure_required", "Acknowledge the current manifest by its SHA-256.")
    try:
        return bytes.fromhex(ack)
    except ValueError:
        raise RecallError("disclosure_required", "Acknowledge the current manifest by its SHA-256.") from None


def acknowledge(session, source_id: str, ack: str) -> dict:
    """Record the operator's acknowledgement of a planned manifest (stored with its time)."""
    store = require_operator(session).store
    digest = _ack_bytes(ack)
    with write_transaction(store.db) as db:
        now = store.clock()
        return _record_ack(db, now, source_id, digest)


def _record_ack(db, now: int, source_id: str, digest: bytes) -> dict:
    row = db.execute("SELECT acknowledged_at FROM extraction_manifests WHERE manifest_hash = ? AND source_id = ?",
                     (digest, source_id)).fetchone()
    if row is None:
        raise RecallError("disclosure_required", "No planned manifest has this hash; run plan first.")
    at = row["acknowledged_at"]
    if at is None:
        at = now
        db.execute("UPDATE extraction_manifests SET acknowledged_at = ? WHERE manifest_hash = ?", (now, digest))
        project = db.execute("SELECT project_id FROM source_settings WHERE source_id = ?", (source_id,)).fetchone()[0]
        audit.append(db, at=now, actor=sources.OPERATOR, action="extract_ack", project_id=project)
    return {"source_id": source_id, "manifest_hash": digest.hex(), "acknowledged_at": timefmt.format_rfc3339(at)}


# -- model output ----------------------------------------------------------------------------------

def _note_key(body: str) -> str:
    return "x." + normalise.body_hash(body).hex()[:32]


def parse_output(output: Any, cfg: sources.SourceSettings) -> tuple[list, int]:
    """Validate one chunk's model output. Returns ([(SaveRequest, confidence)], discarded count).
    Any invalid candidate makes the whole output invalid (ValueError or RecallError): the chunk fails."""
    if isinstance(output, dict) and set(output) == {"candidates"}:
        output = output["candidates"]
    if not isinstance(output, list):
        raise ValueError("output must be a list of candidates")
    kept, discarded = [], 0
    for cand in output:
        if not isinstance(cand, dict) or not set(cand) <= OUTPUT_FIELDS or "kind" not in cand:
            raise ValueError("bad candidate shape")
        confidence = cand.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("bad confidence")
        valid_from = cand.get("valid_from")
        scope, project = cfg.scope_kind, cfg.project_id
        if cand["kind"] == "fact":
            if cand.get("body") is not None or not {"subject", "predicate", "value"} <= set(cand):
                raise ValueError("bad fact")
            req = core_model.validate_save(kind="fact", scope=scope, project=project, valid_from=valid_from,
                                           fact={"subject": cand["subject"], "predicate": cand["predicate"],
                                                 "value": cand["value"]})
        elif cand["kind"] == "note":
            if any(cand.get(f) is not None for f in ("subject", "predicate", "value")):
                raise ValueError("bad note")
            body = cand.get("body")
            if not isinstance(body, str):
                raise ValueError("bad note body")
            req = core_model.validate_save(kind="note", scope=scope, project=project, key=_note_key(body),
                                           body=body, valid_from=valid_from)
        else:
            raise ValueError("bad kind")
        if confidence < cfg.min_confidence:
            discarded += 1
            continue
        kept.append((req, float(confidence)))
    return kept, discarded


# -- run -------------------------------------------------------------------------------------------

def _status(db, run_id: str) -> str:
    return db.execute("SELECT status FROM extraction_runs WHERE id = ?", (run_id,)).fetchone()[0]


def _day_used(db, source_id: str, now: int) -> int:
    return db.execute("SELECT COALESCE(SUM(tokens_used), 0) FROM extraction_runs WHERE source_id = ?"
                      " AND started_at >= ?", (source_id, sources.day_start(now))).fetchone()[0]


def run(session, source_id: str, *, ack: Optional[str], model: ExtractionModel) -> dict:
    """Run extraction for one source under an acknowledged manifest. Returns the run summary; its
    `status` is done, cancelled or budget_stopped (failed only on an unexpected error)."""
    store = require_operator(session).store
    with read_transaction(store.db) as db:
        cfg = sources.load(db, source_id)
    if not cfg.enabled:
        raise RecallError("forbidden", "Extraction is not enabled for this source.")
    _check_privacy(cfg)
    if cfg.endpoint is None or cfg.model_id is None:
        raise invalid("Configure an endpoint and model_id before running.")
    if getattr(model, "endpoint", None) != cfg.endpoint or getattr(model, "model_id", None) != cfg.model_id:
        raise RecallError("disclosure_required", "The model client does not match the acknowledged endpoint and model.")
    if getattr(model, "is_local", False) is not True and not cfg.cloud_allowed:
        # A client that does not call itself local is treated as non-local, whatever its endpoint says.
        raise RecallError("disclosure_required", "A non-local client needs cloud_allowed on the source.")
    digest = _ack_bytes(ack)
    with read_transaction(store.db) as db:
        manifest, pending = _build(db, cfg)
    if manifest_hash(manifest) != digest:
        raise RecallError("disclosure_required", "The manifest changed; plan again and acknowledge the new hash.")

    with write_transaction(store.db) as db:
        now = store.clock()
        _record_ack(db, now, source_id, digest)
        if db.execute("SELECT 1 FROM extraction_runs WHERE source_id = ? AND status = 'running'",
                      (source_id,)).fetchone():
            raise RecallError("conflict", "A run of this source is in progress.")
        run_id = store._ulid(now)
        db.execute(
            "INSERT INTO extraction_runs (id, source_id, model_id, endpoint, manifest_json, manifest_hash, tokens_used,"
            " started_at, status) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'running')",
            (run_id, source_id, cfg.model_id, cfg.endpoint, normalise.canonical_json(manifest), digest, now))
        audit.append(db, at=now, actor=actor_for(run_id), action="extract_start", project_id=cfg.project_id)

    summary = {"run_id": run_id, "source_id": source_id, "manifest_hash": digest.hex(), "status": "running",
               "chunks_total": manifest["chunk_count"], "chunks_skipped": manifest["chunk_count"] - len(pending),
               "chunks_done": 0, "chunks_failed": 0, "chunks_not_run": 0, "candidates_written": 0,
               "candidates_discarded": 0, "tokens_used": 0, "stop_reason": None}
    final = "done"
    try:
        for index, chunk in enumerate(pending):
            with read_transaction(store.db) as db:
                if _status(db, run_id) == "cancelled":
                    final = "cancelled"
                    summary["chunks_not_run"] = len(pending) - index
                    break
                day_used = _day_used(db, source_id, store.clock())
            if summary["tokens_used"] + chunk.tokens > cfg.run_token_budget:
                final, summary["stop_reason"] = "budget_stopped", "run_budget"
            elif day_used + chunk.tokens > cfg.day_token_budget:
                final, summary["stop_reason"] = "budget_stopped", "day_budget"
            if final == "budget_stopped":
                summary["chunks_not_run"] = len(pending) - index
                break
            _process(store, cfg, run_id, chunk, model, summary)
        else:
            with read_transaction(store.db) as db:
                if _status(db, run_id) == "cancelled":
                    final = "cancelled"
    except BaseException:
        final = "failed"
        raise
    finally:
        with write_transaction(store.db) as db:
            now = store.clock()
            status = _status(db, run_id)
            if status == "running":
                status = final
            db.execute("UPDATE extraction_runs SET status = ?, ended_at = ? WHERE id = ?", (status, now, run_id))
            audit.append(db, at=now, actor=actor_for(run_id), action="extract_end", project_id=cfg.project_id)
        summary["status"] = status
    return summary


def _process(store, cfg: sources.SourceSettings, run_id: str, chunk: chunking.Chunk, model, summary: dict) -> None:
    """One model call for one chunk, then one write transaction for everything it produced."""
    try:
        output = model.extract(chunk.text)
        kept, discarded = parse_output(output, cfg)
        failed = False
    except (ValueError, TypeError, KeyError, RecallError):
        kept, discarded, failed = [], 0, True
    summary["tokens_used"] += chunk.tokens
    actor = actor_for(run_id)

    def record(db, now: int, status: str) -> None:
        db.execute("INSERT OR REPLACE INTO extraction_chunks (chunk_hash, model_id, template_version, run_id, status)"
                   " VALUES (?, ?, ?, ?, ?)", (chunk.hash, cfg.model_id, chunking.TEMPLATE_VERSION, run_id, status))
        db.execute("UPDATE extraction_runs SET tokens_used = tokens_used + ? WHERE id = ?", (chunk.tokens, run_id))
        audit.append(db, at=now, actor=actor, action="extract_chunk", project_id=cfg.project_id)

    if not failed:
        try:
            with write_transaction(store.db) as db:
                now = store.clock()
                for req, confidence in kept:
                    saved = store._save(db, cfg.owner_id, req, None, now, actor=actor, candidate_only=True)
                    db.execute("INSERT OR IGNORE INTO provenance (item_id, rev, source_id, chunk_hash, run_id, confidence)"
                               " VALUES (?, ?, ?, ?, ?, ?)",
                               (saved["item_id"], saved["rev"], cfg.source_id, chunk.hash, run_id, confidence))
                record(db, now, "done")
            summary["chunks_done"] += 1
            summary["candidates_written"] += len(kept)
            summary["candidates_discarded"] += discarded
            return
        except RecallError:
            pass
    with write_transaction(store.db) as db:
        record(db, store.clock(), "failed")
    summary["chunks_failed"] += 1


def cancel(session, run_id: str) -> dict:
    """Ask a running run to stop; it stops before its next chunk. Output written so far stays as candidates."""
    store = require_operator(session).store
    with write_transaction(store.db) as db:
        now = store.clock()
        row = db.execute("SELECT r.status, c.project_id FROM extraction_runs AS r JOIN source_settings AS c"
                         " ON c.source_id = r.source_id WHERE r.id = ?", (run_id,)).fetchone()
        if row is None:
            raise RecallError("not_found", "No such run.")
        if row["status"] == "running":
            db.execute("UPDATE extraction_runs SET status = 'cancelled' WHERE id = ?", (run_id,))
            audit.append(db, at=now, actor=sources.OPERATOR, action="extract_cancel", project_id=row["project_id"])
        return {"run_id": run_id, "status": _status(db, run_id)}


def list_runs(session, source_id: Optional[str] = None) -> list[dict]:
    store = require_operator(session).store
    with read_transaction(store.db) as db:
        rows = db.execute(
            "SELECT id, source_id, model_id, endpoint, manifest_hash, tokens_used, started_at, ended_at, status"
            " FROM extraction_runs WHERE (:s IS NULL OR source_id = :s) ORDER BY started_at, id", {"s": source_id})
        out = []
        for row in rows:
            chunks = dict(db.execute("SELECT status, COUNT(*) FROM extraction_chunks WHERE run_id = ? GROUP BY status",
                                     (row["id"],)).fetchall())
            candidates = db.execute("SELECT COUNT(*) FROM provenance WHERE run_id = ?", (row["id"],)).fetchone()[0]
            out.append({
                "run_id": row["id"], "source_id": row["source_id"], "model_id": row["model_id"],
                "endpoint": row["endpoint"], "manifest_hash": row["manifest_hash"].hex(),
                "tokens_used": row["tokens_used"], "status": row["status"],
                "started_at": timefmt.format_rfc3339(row["started_at"]),
                "ended_at": None if row["ended_at"] is None else timefmt.format_rfc3339(row["ended_at"]),
                "chunks_done": chunks.get("done", 0), "chunks_failed": chunks.get("failed", 0),
                "candidates": candidates})
        return out


def manifest_of_run(session, run_id: str) -> dict:
    """The manifest stored with a run (manifests live with their runs)."""
    store = require_operator(session).store
    with read_transaction(store.db) as db:
        row = db.execute("SELECT manifest_json, manifest_hash FROM extraction_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RecallError("not_found", "No such run.")
        return {"manifest": json.loads(row["manifest_json"]), "manifest_hash": row["manifest_hash"].hex()}
