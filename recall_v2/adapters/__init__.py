"""Ingestion adapters (section 8). Never imported by core.

Adapters read local sources into records for extraction or explicit saves. Platform-specific formats
(for example coding-agent session files) need real sample files and are not part of this package yet;
the generic JSONL message-log reader covers any one-message-per-line log through configurable fields.
"""
from .base import Record, SourceAdapter, SourceRef
from .jsonl import JsonlMessageLog

ADAPTERS = {JsonlMessageLog.name: JsonlMessageLog}


def get_adapter(name: str, config=None) -> SourceAdapter:
    try:
        cls = ADAPTERS[name]
    except KeyError:
        raise KeyError(name) from None
    return cls(config)


__all__ = ["ADAPTERS", "JsonlMessageLog", "Record", "SourceAdapter", "SourceRef", "get_adapter"]
