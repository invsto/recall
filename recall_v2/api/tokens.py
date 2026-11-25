"""Token files (section 5): a 0600, operator-owned file holding one bearer token.

A principal's identity comes only from its token. Interfaces read the token from a file (CLI
`--token-file`, MCP stdio `RECALL_TOKEN_FILE`) or from an `Authorization: Bearer` header (HTTP); never
from tool or command arguments.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Optional, Union

from ..core.errors import RecallError

MAX_TOKEN_BYTES = 256
BEARER = re.compile(r"(?i:bearer) +(\S+)")


def read_token_file(path: Union[str, Path]) -> str:
    path = Path(path).expanduser()
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise RecallError("unauthorized", "The token file does not exist.") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or info.st_nlink != 1):
        raise RecallError("unsafe_path", "The token file must be an operator-owned 0600 regular file.")
    with open(path, "rb") as handle:
        raw = handle.read(MAX_TOKEN_BYTES + 1)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise RecallError("unauthorized", "A current token is required.") from None
    if not token or len(raw) > MAX_TOKEN_BYTES:
        raise RecallError("unauthorized", "A current token is required.")
    return token


def write_token_file(path: Union[str, Path], token: str) -> Path:
    """Write a token to a new 0600 file; refuse an existing path."""
    path = Path(path).expanduser().absolute()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RecallError("backup_exists", "The destination already exists.") from None
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(token + "\n")
    return path


def bearer_token(header_values: list[str]) -> Optional[str]:
    """The token of exactly one `Authorization: Bearer <token>` header, else None."""
    if len(header_values) != 1:
        return None
    match = BEARER.fullmatch(header_values[0].strip())
    return match[1] if match else None
