"""Extraction precision from owner labels (section 9.1: correct candidates / all labelled candidates).

Label file format: JSONL, one labelled candidate per line:

    {"chunk_hash": "<64 hex>", "label": "accepted" | "rejected", "note": "<optional text>"}

Optional fields: "item_id" and "rev" (which candidate of the chunk), "synthetic": true (test data, not
owner labels). Blank lines are ignored. Labels stay local; this reporter only reads the file.

    python -m recall_v2.extraction.precision [--labels PATH] [--check] [--json]

With no file (no --labels, or a path that does not exist) it prints "no labels supplied" and exits 0:
nothing is measured and no label is invented. A malformed file exits 2. --check exits 1 when precision
is below the v2.0 threshold.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Optional

THRESHOLD = 0.70
LABELS = ("accepted", "rejected")
FIELDS = {"chunk_hash", "label", "note", "item_id", "rev", "synthetic"}
HEX64 = re.compile(r"[0-9a-f]{64}")
NO_LABELS = "no labels supplied"


class LabelError(ValueError):
    pass


def read_labels(path: Path) -> list[dict]:
    labels = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise LabelError(f"line {number}: not JSON") from None
        if not isinstance(obj, dict) or not {"chunk_hash", "label"} <= set(obj) or not set(obj) <= FIELDS:
            raise LabelError(f"line {number}: needs chunk_hash and label, and no other fields than {sorted(FIELDS)}")
        if not isinstance(obj["chunk_hash"], str) or not HEX64.fullmatch(obj["chunk_hash"]):
            raise LabelError(f"line {number}: chunk_hash must be 64 lower-case hex characters")
        if obj["label"] not in LABELS:
            raise LabelError(f"line {number}: label must be accepted or rejected")
        if "note" in obj and not isinstance(obj["note"], str):
            raise LabelError(f"line {number}: note must be text")
        if "synthetic" in obj and not isinstance(obj["synthetic"], bool):
            raise LabelError(f"line {number}: synthetic must be a boolean")
        labels.append(obj)
    return labels


def report(labels: list[dict]) -> dict:
    accepted = sum(1 for label in labels if label["label"] == "accepted")
    total = len(labels)
    precision = None if total == 0 else accepted / total
    return {
        "labelled": total, "accepted": accepted, "rejected": total - accepted,
        "chunks": len({label["chunk_hash"] for label in labels}),
        "precision": precision, "threshold": THRESHOLD,
        "meets_threshold": None if precision is None else precision >= THRESHOLD,
        "synthetic": any(label.get("synthetic") for label in labels),
    }


def main(argv: Optional[list] = None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m recall_v2.extraction.precision",
                                     description="Extraction precision from a local owner-labels file.")
    parser.add_argument("--labels", type=Path, default=None, help="JSONL labels file")
    parser.add_argument("--check", action="store_true", help="exit 1 if precision is below the threshold")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if args.labels is None or not args.labels.is_file():
        print(NO_LABELS, file=out)
        return 0
    try:
        labels = read_labels(args.labels)
    except (LabelError, UnicodeDecodeError) as error:
        print(f"invalid labels file: {error}", file=out)
        return 2
    if not labels:
        print(NO_LABELS, file=out)
        return 0
    result = report(labels)
    if args.json:
        print(json.dumps(result, sort_keys=True), file=out)
    else:
        kind = "SYNTHETIC labels (not owner labels)" if result["synthetic"] else "labels"
        verdict = "meets" if result["meets_threshold"] else "below"
        print(f"precision {result['precision']:.4f} ({result['accepted']}/{result['labelled']} {kind}, "
              f"{result['chunks']} chunks); threshold {THRESHOLD:.2f}: {verdict}", file=out)
    return 1 if args.check and not result["meets_threshold"] else 0


if __name__ == "__main__":
    sys.exit(main())
