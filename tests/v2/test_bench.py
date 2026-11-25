"""Benchmark harness: gates pass on the default seed (reduced size); the evaluator catches wrong answers."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from recall_v2.bench import DEFAULT_SEED, GENERATOR_VERSION, reference, run
from recall_v2.bench.__main__ import main
from recall_v2.bench.generator import generate


class BenchTests(unittest.TestCase):
    def test_generator_is_seeded(self):
        first, second = generate(DEFAULT_SEED, 50), generate(DEFAULT_SEED, 50)
        self.assertEqual(repr(first.chains), repr(second.chains))
        self.assertNotEqual(repr(first.chains), repr(generate(DEFAULT_SEED + 1, 50).chains))
        self.assertEqual(first.version, GENERATOR_VERSION)
        self.assertTrue(all(1 <= sum(e.op == "assert" for e in c.events) for c in first.chains))

    def test_gates_pass_on_default_seed_reduced(self):
        metrics = run(seed=DEFAULT_SEED, chains=300, text_queries=100)
        self.assertTrue(metrics["passed"], metrics)
        self.assertEqual((metrics["seed"], metrics["generator_version"]), (DEFAULT_SEED, GENERATOR_VERSION))
        self.assertEqual(metrics["current_truth_accuracy"], 1.0)
        self.assertEqual((metrics["stale_current"], metrics["scope_leaks"]), (0, 0))
        events = metrics["events"]
        self.assertTrue(events["retract"] and events["backdated"] and events["expiring"] and events["delete"])

    def test_wrong_truth_fails_the_gates(self):
        original = reference.current

        def ignores_retractions(ref, tv, tk):
            kept = reference.RefChain([r for r in ref.revisions if r.op != "retract"], ref.deleted)
            return original(kept, tv, tk)

        with mock.patch.object(reference, "current", ignores_retractions):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--check", "--chains", "150", "--text-queries", "50"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out.getvalue())


if __name__ == "__main__":
    unittest.main()
