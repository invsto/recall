"""Budgets (spec 3.2): a run stops before the call that would exceed its per-run or per-day cap."""
from __future__ import annotations

import unittest

from recall_v2.extraction.chunking import MAX_CHUNK_BYTES, estimate_tokens

from .extraction_helpers import ExtractionCase
from .governance_helpers import MICROS_PER_DAY


def big_log(n: int, size: int = 4000, tag: str = "a"):
    """n records that each fill one chunk on their own (two never fit in 6,000 bytes)."""
    return [("user", f"remember: item {tag}{i} " + "x" * size) for i in range(n)]


class BudgetTests(ExtractionCase):
    def chunk_tokens(self, planned) -> int:
        manifest = planned["manifest"]
        self.assertEqual(manifest["pending_chunk_count"], manifest["chunk_count"])
        # All chunks here have the same size, so each costs the same.
        return manifest["estimated_tokens"] // manifest["pending_chunk_count"]

    def test_run_stops_at_its_cap(self):
        source = self.add_source(self.log(big_log(6)), run_token_budget=4_000)
        planned = self.plan(source)
        per_chunk = self.chunk_tokens(planned)
        self.assertTrue(0 < 4000 // per_chunk < 6)   # the cap falls inside the run
        model = self.model()
        error = self.err(self.op.extract_run(source, planned["manifest_hash"], model=model),
                         "extraction_budget_exceeded")
        details = error["details"]
        expected_calls = 4000 // per_chunk
        self.assertEqual(details["status"], "budget_stopped")
        self.assertEqual(details["stop_reason"], "run_budget")
        self.assertEqual(len(model.calls), expected_calls)
        self.assertEqual(details["chunks_done"], expected_calls)
        self.assertEqual(details["tokens_used"], expected_calls * per_chunk)
        self.assertLessEqual(details["tokens_used"], 4_000)                   # never beyond the cap
        self.assertGreater(details["tokens_used"] + per_chunk, 4_000)        # the next call would exceed it
        self.assertEqual(details["chunks_not_run"], 6 - expected_calls)
        row = self.store.db.execute("SELECT status, tokens_used, ended_at FROM extraction_runs").fetchone()
        self.assertEqual((row["status"], row["tokens_used"]), ("budget_stopped", details["tokens_used"]))
        self.assertIsNotNone(row["ended_at"])
        # Candidates written before the stop are kept.
        self.assertEqual(len(self.revisions_of_run(details["run_id"])), expected_calls)
        # Tokens actually sent (estimator over template + received text) stay within the cap.
        self.assertLessEqual(sum(estimate_tokens(text) for text in model.calls), 4_000)
        self.assertTrue(all(len(text.encode("utf-8")) <= MAX_CHUNK_BYTES for text in model.calls))

    def test_daily_cap_across_two_runs(self):
        path = self.log(big_log(3, tag="a"))
        source = self.add_source(path, day_token_budget=6_000)
        first = self.plan(source)
        per_chunk = self.chunk_tokens(first)
        model = self.model()
        run1 = self.ok(self.op.extract_run(source, first["manifest_hash"], model=model))
        self.assertEqual((run1["status"], run1["tokens_used"]), ("done", 3 * per_chunk))

        # New content the same day: only what is left of the daily cap can be spent.
        self.log(big_log(3, tag="a") + big_log(3, tag="b"))
        second = self.plan(source)
        self.assertEqual(second["manifest"]["pending_chunk_count"], 3)
        error = self.err(self.op.extract_run(source, second["manifest_hash"], model=model),
                         "extraction_budget_exceeded")
        allowed = (6_000 - 3 * per_chunk) // per_chunk
        self.assertEqual(error["details"]["stop_reason"], "day_budget")
        self.assertEqual(error["details"]["chunks_done"], allowed)
        self.assertEqual(len(model.calls), 3 + allowed)
        day_total = self.store.db.execute("SELECT SUM(tokens_used) FROM extraction_runs").fetchone()[0]
        self.assertLessEqual(day_total, 6_000)
        self.assertGreater(day_total + per_chunk, 6_000)

        # The next UTC day has a fresh allowance.
        self.clock.now += MICROS_PER_DAY
        third = self.plan(source)
        run3 = self.ok(self.op.extract_run(source, third["manifest_hash"], model=model))
        self.assertEqual(run3["chunks_done"], 3 - allowed)


if __name__ == "__main__":
    unittest.main()
