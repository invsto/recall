"""Normalised text, fact keys, canonical JSON, value equality, body normalisation and ID rules."""
from __future__ import annotations

from hashlib import sha256
import json
import re
import unicodedata

from .errors import invalid

FACT_KEY_SEPARATOR = "\u001f"
NORM_MAX_BYTES = 256

# The Unicode White_Space property (not str.isspace, which also admits U+001C..U+001F).
WHITE_SPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d \u0085  "
    "           "
    "    　"
)

PRINCIPAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
PROJECT_ID = PRINCIPAL_ID
ITEM_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
REQUEST_ID = ITEM_KEY


def _collapse_whitespace(text: str) -> str:
    out: list[str] = []
    in_space = False
    for char in text:
        if char in WHITE_SPACE:
            if not in_space:
                out.append(" ")
            in_space = True
        else:
            out.append(char)
            in_space = False
    return "".join(out).strip(" ")


def norm(text: str) -> str:
    """NFKC, casefold, collapse every run of Unicode whitespace to one space, trim.

    NFKC is applied once more after casefold: casefold can leave a decomposed sequence (for example
    U+00DF followed by U+0301 becomes "ss" + U+0301), and without the second pass norm would not be
    idempotent.
    """
    if not isinstance(text, str):
        raise invalid("Expected a string.")
    for char in text:
        if char not in WHITE_SPACE and unicodedata.category(char) in ("Cc", "Cf"):
            raise invalid("Control and format characters are not allowed.")
    result = _collapse_whitespace(unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold()))
    if not result:
        raise invalid("The value is empty after normalising.")
    if len(result.encode("utf-8")) > NORM_MAX_BYTES:
        raise invalid("The value exceeds 256 bytes after normalising.")
    return result


def fact_key(subject: str, predicate: str) -> str:
    """The fact item's key column: norm(subject) U+001F norm(predicate)."""
    return norm(subject) + FACT_KEY_SEPARATOR + norm(predicate)


def split_fact_key(key: str) -> tuple[str, str]:
    subject, _, predicate = key.partition(FACT_KEY_SEPARATOR)
    return subject, predicate


def canonical_json(value) -> str:
    """Sorted keys, separators ',' and ':', no ASCII escaping, no NaN."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _nfc_strings(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc_strings(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise invalid("JSON object keys must be strings.")
            out[unicodedata.normalize("NFC", key)] = _nfc_strings(item)
        return out
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise invalid("The value must be JSON.")


def canonical_value(value) -> str:
    """Canonical JSON of a fact value with every string NFC-normalised. Case is significant."""
    try:
        return canonical_json(_nfc_strings(value))
    except ValueError:
        raise invalid("The value must be finite JSON.") from None


def values_equal(left, right) -> bool:
    return canonical_value(left) == canonical_value(right)


def normalise_body(text: str) -> str:
    """NFC; CRLF and CR become LF; trailing spaces per line removed; trim."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip(" ") for line in text.split("\n")).strip()


def body_hash(text: str) -> bytes:
    return sha256(normalise_body(text).encode("utf-8")).digest()


def check_id(value, pattern: re.Pattern, what: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise invalid(f"{what} has an invalid format.")
    return value
