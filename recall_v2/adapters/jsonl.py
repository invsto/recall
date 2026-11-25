"""A generic JSONL message-log reader: one JSON object per line with role, text, timestamp and session fields.

Field names are configurable, so any log that writes one message object per line can be read without a
format-specific adapter. The reader is read-only: it reads each file's bytes once per call and never
writes, renames or touches it. Lines that are not JSON objects, or whose text field is not a non-empty
string, are skipped.

Config: {"path": file or directory (non-recursive, *.jsonl, sorted), "fields": {"role", "text",
"timestamp", "session"}, "categories": the roles this log may contain}.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .base import Record, SourceAdapter, SourceRef

DEFAULT_FIELDS = {"role": "role", "text": "text", "timestamp": "timestamp", "session": "session_id"}
DEFAULT_CATEGORIES = ("user", "assistant", "system", "tool", "unknown")


class JsonlMessageLog(SourceAdapter):
    name = "jsonl-messages"

    def __init__(self, config: Optional[Mapping[str, Any]] = None):
        super().__init__(config)
        fields = dict(DEFAULT_FIELDS)
        fields.update(self.config.get("fields") or {})
        if set(fields) != set(DEFAULT_FIELDS) or not all(isinstance(v, str) and v for v in fields.values()):
            raise ValueError("fields must map role, text, timestamp and session to field names")
        self.fields = fields
        self.categories = tuple(self.config.get("categories") or DEFAULT_CATEGORIES)

    def _paths(self, config: Mapping[str, Any]) -> list[Path]:
        path = Path(config["path"]).expanduser()
        if path.is_dir():
            return sorted(p for p in path.glob("*.jsonl") if p.is_file())
        return [path] if path.is_file() else []

    def discover(self, config: Optional[Mapping[str, Any]] = None) -> Iterable[SourceRef]:
        config = self.config if config is None else config
        refs = []
        for path in self._paths(config):
            data = path.read_bytes()
            refs.append(SourceRef(uri=str(path.absolute()), content_hash=sha256(data).digest(), size=len(data)))
        return refs

    def read(self, ref: SourceRef) -> Iterable[Record]:
        data = Path(ref.uri).read_bytes()
        records = []
        for number, line in enumerate(data.decode("utf-8", "replace").split("\n"), start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            text = obj.get(self.fields["text"])
            if not isinstance(text, str) or not text.strip():
                continue
            role = obj.get(self.fields["role"])
            ts = obj.get(self.fields["timestamp"])
            session = obj.get(self.fields["session"])
            records.append(Record(
                text=text, role=role if isinstance(role, str) else None,
                ts=None if ts is None else str(ts), uri=ref.uri,
                meta={"line": number, "session_id": None if session is None else str(session)}))
        return records

    def describe(self) -> dict:
        return {"adapter": self.name, "fields": ["text", "role", "ts", "uri", "meta.line", "meta.session_id"],
                "source_fields": dict(self.fields), "categories": list(self.categories)}
