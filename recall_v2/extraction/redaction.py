"""Deterministic, regex-based redaction applied to every record before any model call (section 3.2).

Rules `secrets`, `emails` and `private_tags` are on by default. Each match becomes `[REDACTED:<rule>]`.
Rules run in a fixed order (private tags, then secrets, then emails, then custom rules in the order
given), so the same text and settings always give the same output and the same counts. Counts are
content-free and go into the disclosure manifest.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

from ..core.errors import invalid

BUILTIN_RULES = ("private_tags", "secrets", "emails")
RULE_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")

_PRIVATE_TAGS = [
    re.compile(r"<private>.*?</private>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<private>.*\Z", re.IGNORECASE | re.DOTALL),   # an unclosed tag hides the rest
]

_SECRETS = [
    # PEM private key blocks.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.DOTALL),
    # Credentials embedded in a URL (scheme://user:password@host).
    re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)"),
    # Authorization headers.
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}"),
    # Assignments to secret-like names: password=..., api_key: ..., "token": "...".
    re.compile(r"""(?ix)\b[\w.-]*(?:passw(?:or)?d|passphrase|secret|api[_-]?key|access[_-]?key|auth[_-]?token
                   |token|credential|private[_-]?key)[\w.-]*["']?\s*[:=]\s*["']?[^\s"',;]{4,}["']?"""),
    # Vendor-style key prefixes.
    re.compile(r"\b(?:sk|pk|rk)-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    # JSON Web Tokens.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
]

_EMAILS = [re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")]

_BUILTIN = {"private_tags": _PRIVATE_TAGS, "secrets": _SECRETS, "emails": _EMAILS}

DEFAULT_SETTINGS = {"private_tags": True, "secrets": True, "emails": True, "custom": []}


def normalise_settings(settings: Mapping | None) -> dict:
    """Validate redaction settings; missing built-in rules default to on."""
    merged = dict(DEFAULT_SETTINGS)
    if settings is not None:
        if not isinstance(settings, Mapping):
            raise invalid("redaction must be an object.")
        unknown = set(settings) - set(DEFAULT_SETTINGS)
        if unknown:
            raise invalid("Unknown redaction setting.", fields=sorted(unknown))
        merged.update(settings)
    for name in BUILTIN_RULES:
        if not isinstance(merged[name], bool):
            raise invalid(f"redaction.{name} must be a boolean.")
    custom = merged["custom"]
    if not isinstance(custom, list):
        raise invalid("redaction.custom must be a list.")
    seen = set(BUILTIN_RULES)
    out = []
    for rule in custom:
        if not isinstance(rule, Mapping) or set(rule) != {"name", "pattern"}:
            raise invalid("A custom redaction rule has exactly name and pattern.")
        name, pattern = rule["name"], rule["pattern"]
        if not isinstance(name, str) or not RULE_NAME.fullmatch(name) or name in seen:
            raise invalid("A custom redaction rule needs a new lower-case name.")
        if not isinstance(pattern, str) or not pattern:
            raise invalid("A custom redaction pattern must be a non-empty string.")
        try:
            compiled = re.compile(pattern)
        except re.error:
            raise invalid("A custom redaction pattern is not a valid regular expression.") from None
        if compiled.match(""):
            raise invalid("A custom redaction pattern must not match empty text.")
        seen.add(name)
        out.append({"name": name, "pattern": pattern})
    merged["custom"] = out
    return merged


class Redactor:
    def __init__(self, settings: Mapping | None = None):
        self.settings = normalise_settings(settings)
        self.rules: list[tuple[str, list[re.Pattern]]] = [
            (name, _BUILTIN[name]) for name in BUILTIN_RULES if self.settings[name]]
        self.rules += [(rule["name"], [re.compile(rule["pattern"])]) for rule in self.settings["custom"]]

    @property
    def rule_names(self) -> list[str]:
        return [name for name, _ in self.rules]

    def redact(self, text: str) -> tuple[str, dict[str, int]]:
        """Return the redacted text and the number of matches per rule."""
        counts = {name: 0 for name, _ in self.rules}
        for name, patterns in self.rules:
            marker = f"[REDACTED:{name}]"
            for pattern in patterns:
                text, n = pattern.subn(marker, text)
                counts[name] += n
        return text, counts


def add_counts(total: dict[str, int], counts: Mapping[str, int]) -> None:
    for name, n in counts.items():
        total[name] = total.get(name, 0) + n


def redact_all(redactor: Redactor, texts: Iterable[str]) -> tuple[list[str], dict[str, int]]:
    total = {name: 0 for name in redactor.rule_names}
    out = []
    for text in texts:
        clean, counts = redactor.redact(text)
        add_counts(total, counts)
        out.append(clean)
    return out, total
