"""Read a single configured Recall context for an agent handoff."""
from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from .context_policy import MAX_CONTEXT_CHARS, READING_BOUNDARY, sections, validate_context

MAX_DOCUMENT_BYTES = MAX_CONTEXT_CHARS * 4
MAX_BRIEF_BYTES = 4_096


def _read_document(path: Path) -> str:
    if not path.is_absolute() or path.anchor != "/" or path.suffix != ".md" or ".." in path.parts:
        raise ValueError("configure an absolute, canonical Markdown file path")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("context briefs require a platform with symlink-safe file opens")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, directory_flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        file_descriptor = os.open(path.name, file_flags, dir_fd=descriptor)
        with os.fdopen(file_descriptor, "rb") as document:
            if not stat.S_ISREG(os.fstat(document.fileno()).st_mode):
                raise ValueError("configured context must be a regular file")
            raw = document.read(MAX_DOCUMENT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("configured context is too large")
    return raw.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context_file", type=Path, help="absolute path fixed by the agent operator")
    args = parser.parse_args(argv)
    try:
        document = _read_document(args.context_file)
    except (OSError, UnicodeError):
        parser.exit(1, "error: cannot read the configured context as UTF-8 without following symlinks\n")
    except ValueError as error:
        parser.exit(1, f"error: {error}\n")
    try:
        report = validate_context(document, require_structured=True)
        parts = sections(document)
    except ValueError:
        parser.exit(1, "error: configured context must be a valid structured Recall document\n")

    next_actions = parts["Open questions and next actions"]
    if report["schema_version"] == 2:
        next_actions = next_actions.removesuffix(READING_BOUNDARY).strip()
    brief = json.dumps({
        "source": args.context_file.name,
        "summary": parts["Summary"],
        "next_actions": next_actions,
    })
    if len(brief) + 1 > MAX_BRIEF_BYTES:
        parser.exit(1, "error: context summary and next actions exceed the 4096-byte brief limit\n")
    print(brief)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
