"""The extraction model interface and the only two implementations this build ships.

`ExtractionModel` is what a run calls once per chunk: `extract(chunk_text)` returns a list of candidate
dicts of the section 3.2 output shape (`kind` "fact" or "note"; facts carry subject, predicate, value;
notes carry body; optional valid_from; confidence in [0, 1]). The run never trusts `is_local` alone:
locality is derived from the configured endpoint, and the client's endpoint and model ID must equal the
ones in the acknowledged manifest.

No network or command-line client exists here. `RuleBasedExtractor` is a deterministic, in-process
extractor; `RecordingModel` is a test double that records the exact text it receives.
"""
from __future__ import annotations

import re
from typing import Callable, Optional, Protocol, runtime_checkable


@runtime_checkable
class ExtractionModel(Protocol):
    endpoint: str
    model_id: str
    is_local: bool

    def extract(self, chunk_text: str) -> list[dict]:
        ...


# "subject | predicate = value" on one line.
_FACT = re.compile(r"^(?:[A-Za-z_-]+: )?(?P<subject>[^|\n=]+?)\s*\|\s*(?P<predicate>[^|\n=]+?)\s*=\s*(?P<value>[^\n]+?)\s*$",
                   re.MULTILINE)
# "remember: ...", "remember that ...", "note: ..." (case-insensitive).
_NOTE = re.compile(r"(?i)\b(?:remember(?: that)?|note)\s*[:,]?\s+(?P<body>[^\n]+?)\s*$", re.MULTILINE)
# A hedge lowers confidence below the default minimum.
_HEDGE = re.compile(r"(?i)\b(?:maybe|perhaps|not sure)\b")


class RuleBasedExtractor:
    """Deterministic local extractor: `subject | predicate = value` lines become facts (confidence 0.9);
    `remember: ...` / `note: ...` lines become notes (0.8, or 0.3 when hedged)."""

    def __init__(self, endpoint: str = "local:rule-extractor", model_id: str = "rules-v1"):
        self.endpoint = endpoint
        self.model_id = model_id
        self.is_local = True

    def extract(self, chunk_text: str) -> list[dict]:
        out = []
        for match in _FACT.finditer(chunk_text):
            out.append({"kind": "fact", "subject": match["subject"], "predicate": match["predicate"],
                        "value": match["value"], "confidence": 0.9})
        for match in _NOTE.finditer(chunk_text):
            body = match["body"]
            out.append({"kind": "note", "body": body, "confidence": 0.3 if _HEDGE.search(body) else 0.8})
        return out


class RecordingModel:
    """Test double: records every text it receives (in `calls`) and answers through `respond`, which
    defaults to the rule-based extractor. `before_call` runs before each answer (tests use it to cancel)."""

    def __init__(self, *, endpoint: str = "local:recording", model_id: str = "recording-v1", is_local: bool = True,
                 respond: Optional[Callable[[str], list]] = None, before_call: Optional[Callable[[int], None]] = None):
        self.endpoint = endpoint
        self.model_id = model_id
        self.is_local = is_local
        self.calls: list[str] = []
        self._respond = respond or RuleBasedExtractor().extract
        self._before_call = before_call

    def extract(self, chunk_text: str) -> list:
        self.calls.append(chunk_text)
        if self._before_call is not None:
            self._before_call(len(self.calls))
        return self._respond(chunk_text)
