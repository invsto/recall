"""python -m recall_v2.bench [--check] [--seed N] [--chains N] [--text-queries N] [--json]"""
from __future__ import annotations

import argparse
import json
import sys

from .evaluate import TEXT_QUERIES, run, summary_line
from .generator import DEFAULT_CHAINS, DEFAULT_SEED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recall_v2.bench", description="Fact-evolution benchmark.")
    parser.add_argument("--check", action="store_true", help="exit 1 if any gate fails")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--chains", type=int, default=DEFAULT_CHAINS)
    parser.add_argument("--text-queries", type=int, default=TEXT_QUERIES)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    metrics = run(seed=args.seed, chains=args.chains, text_queries=args.text_queries)
    if args.json:
        print(json.dumps(metrics, indent=2, sort_keys=True))
    for gate, ok in metrics["gates"].items():
        print(f"gate {gate}: {'pass' if ok else 'FAIL'}")
    print(summary_line(metrics))
    return 1 if args.check and not metrics["passed"] else 0


if __name__ == "__main__":
    sys.exit(main())
