"""Shared set-up for the extraction tests: a JSONL message log, a registered source and run helpers.

No sockets, no subprocesses, no model downloads: the only model clients are the in-process rule-based
extractor and the recording test double.
"""
from __future__ import annotations

import json
from pathlib import Path

from recall_v2.extraction.models import RecordingModel

from .governance_helpers import GovernanceCase

LOCAL_ENDPOINT = "local:recording"
MODEL_ID = "recording-v1"


def write_log(path: Path, messages) -> Path:
    """messages: (role, text) pairs, one JSON object per line."""
    with open(path, "w", encoding="utf-8") as handle:
        for n, (role, text) in enumerate(messages):
            handle.write(json.dumps({"role": role, "text": text, "timestamp": f"2026-11-20T10:{n % 60:02d}:00Z",
                                     "session_id": "s-1"}) + "\n")
    return path


BASIC_LOG = [
    ("user", "remember: the staging database lives in region eu-west-2"),
    ("assistant", "deploy-service | owner = team-blue"),
    ("user", "remember that the release train leaves on thursdays"),
    ("system", "remember: system prompts are not a category we extract"),
]


class ExtractionCase(GovernanceCase):
    def setUp(self):
        super().setUp()
        self.logs = self.private_dir()

    def log(self, messages=BASIC_LOG, name: str = "log.jsonl") -> Path:
        return write_log(self.logs / name, messages)

    def add_source(self, path: Path, *, target=None, enabled=True, endpoint=LOCAL_ENDPOINT, model_id=MODEL_ID,
                   **settings) -> str:
        source = self.ok(self.op.source_add(
            adapter="jsonl-messages", config={"path": str(path)},
            target=target or {"scope": "private", "principal": "alice"}, endpoint=endpoint, model_id=model_id,
            **settings))
        self.assertFalse(source["enabled"])   # off by default
        if enabled:
            self.ok(self.op.source_update(source["source_id"], enabled=True))
        return source["source_id"]

    def model(self, **kwargs) -> RecordingModel:
        kwargs.setdefault("endpoint", LOCAL_ENDPOINT)
        kwargs.setdefault("model_id", MODEL_ID)
        return RecordingModel(**kwargs)

    def plan(self, source_id: str) -> dict:
        return self.ok(self.op.extract_plan(source_id))

    def plan_and_run(self, source_id: str, model=None) -> tuple[dict, dict]:
        """Plan, then run under that manifest. Returns (plan, run envelope)."""
        planned = self.plan(source_id)
        model = model or self.model()
        return planned, self.op.extract_run(source_id, planned["manifest_hash"], model=model)

    def revisions_of_run(self, run_id: str) -> list:
        return self.store.db.execute(
            "SELECT r.*, p.source_id, p.chunk_hash, p.run_id, p.confidence, x.model_id FROM provenance AS p"
            " JOIN item_revisions AS r ON r.item_id = p.item_id AND r.rev = p.rev"
            " JOIN extraction_runs AS x ON x.id = p.run_id WHERE p.run_id = ? ORDER BY r.item_id, r.rev",
            (run_id,)).fetchall()
