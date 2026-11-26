"""Fail if platform, company, or product names leak into Recall's generic core.

Scanned: recall_v2/, recall_core/api.py (when present), README.md, and docs/.
Markdown link targets are ignored, so a link may point at an adapter path.
A line tagged `neutral-core: allow` is skipped (used only for the deprecated alias).
Adapters under recall_adapters/ are deliberately not scanned.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("openbot", "invsto", "sharksigma", "grok", "chartworks", "robinhood", "option workstation")
LINK_TARGET = re.compile(r"\]\([^)]*\)")
ALLOW_MARKER = "neutral-core: allow"
SCAN_FILES = ("recall_core/api.py", "README.md")
SCAN_DIRS = (("recall_v2", "*.py"), ("docs", "*.md"))


def scanned_files():
    for name in SCAN_FILES:
        path = ROOT / name
        if path.exists():
            yield path
    for directory, pattern in SCAN_DIRS:
        yield from sorted((ROOT / directory).rglob(pattern))


def main() -> int:
    failures = []
    for path in scanned_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if ALLOW_MARKER in line:
                continue
            text = LINK_TARGET.sub("]", line).casefold()
            for term in FORBIDDEN:
                if term in text:
                    failures.append(f"{path.relative_to(ROOT)}:{number}: forbidden term {term!r}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"neutral core check failed: {len(failures)} issue(s)", file=sys.stderr)
        return 1
    print("neutral core check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
