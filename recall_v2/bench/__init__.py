"""Benchmark harness: the seeded fact-evolution set, its reference truth and the gate checks."""
from .evaluate import run, summary_line
from .generator import DEFAULT_SEED, GENERATOR_VERSION

__all__ = ["DEFAULT_SEED", "GENERATOR_VERSION", "run", "summary_line"]
