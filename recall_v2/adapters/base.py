"""The adapter contract (section 8).

An adapter turns a local source into records. Adapters are read-only (they open files for reading and
never write, rename or touch them), make no network calls, and are never imported by core. Each record
carries a content hash, so the same bytes always yield the same records and hashes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping, Optional


@dataclass(frozen=True)
class SourceRef:
    """One discovered unit of a source (for example one file): where it is and the hash of its bytes."""
    uri: str
    content_hash: bytes
    size: int


@dataclass(frozen=True)
class Record:
    """One record of a source. `role` is the record's category (the unit that allowed categories filter)."""
    text: str
    role: Optional[str]
    ts: Optional[str]
    uri: str
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def category(self) -> str:
        return self.role or "unknown"

    @property
    def hash(self) -> bytes:
        """SHA-256 of the record's canonical JSON (text, role, ts, uri, meta)."""
        payload = json.dumps({"text": self.text, "role": self.role, "ts": self.ts, "uri": self.uri,
                              "meta": dict(self.meta)}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return sha256(payload.encode("utf-8")).digest()


class SourceAdapter(ABC):
    """Base class of every adapter: discover refs, read records, describe what can reach a model."""

    name: str = ""

    def __init__(self, config: Optional[Mapping[str, Any]] = None):
        self.config = dict(config or {})

    @abstractmethod
    def discover(self, config: Optional[Mapping[str, Any]] = None) -> Iterable[SourceRef]:
        """The refs this source currently has, in a stable order."""

    @abstractmethod
    def read(self, ref: SourceRef) -> Iterable[Record]:
        """The records of one ref, in source order. No network; no writes."""

    @abstractmethod
    def describe(self) -> dict:
        """{"adapter", "fields": [...], "categories": [...]}: fields emitted and categories that may reach a model."""
