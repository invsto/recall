"""The labelled-set precision reporter. The owner's real labels do not exist; the fixture is SYNTHETIC."""
from __future__ import annotations

import io
import json
from pathlib import Path
import unittest

from recall_v2.extraction import precision

FIXTURE = Path(__file__).parent / "fixtures" / "extraction_labels_synthetic.jsonl"


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    code = precision.main(list(argv), out=out)
    return code, out.getvalue()


class PrecisionReporterTests(unittest.TestCase):
    def test_absent_file_reports_no_labels_and_exits_zero(self):
        self.assertEqual(run(), (0, "no labels supplied\n"))
        self.assertEqual(run("--labels", str(FIXTURE.parent / "does-not-exist.jsonl")), (0, "no labels supplied\n"))
        self.assertEqual(run("--labels", str(FIXTURE.parent / "does-not-exist.jsonl"), "--check"),
                         (0, "no labels supplied\n"))

    def test_synthetic_labels_match_the_hand_computed_precision(self):
        # Hand count of the fixture: 5 labelled candidates, 3 accepted, 2 rejected, 4 distinct chunks.
        # precision = 3 / 5 = 0.6, below the 0.70 threshold.
        self.assertTrue(all(json.loads(line)["synthetic"] and json.loads(line)["note"].startswith("SYNTHETIC")
                            for line in FIXTURE.read_text().splitlines()))
        code, text = run("--labels", str(FIXTURE), "--json")
        self.assertEqual(code, 0)
        result = json.loads(text)
        self.assertEqual((result["labelled"], result["accepted"], result["rejected"], result["chunks"]), (5, 3, 2, 4))
        self.assertAlmostEqual(result["precision"], 0.6)
        self.assertFalse(result["meets_threshold"])
        self.assertTrue(result["synthetic"])
        code, text = run("--labels", str(FIXTURE))
        self.assertEqual(code, 0)
        self.assertIn("precision 0.6000 (3/5 SYNTHETIC labels", text)
        self.assertEqual(run("--labels", str(FIXTURE), "--check")[0], 1)

    def test_malformed_file_exits_two(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.jsonl"
            for content in ('{"chunk_hash": "zz", "label": "accepted"}\n', '{"chunk_hash": "' + "a" * 64
                            + '", "label": "maybe"}\n', "not json\n"):
                bad.write_text(content, encoding="utf-8")
                self.assertEqual(run("--labels", str(bad))[0], 2)
            bad.write_text("\n\n", encoding="utf-8")
            self.assertEqual(run("--labels", str(bad)), (0, "no labels supplied\n"))


if __name__ == "__main__":
    unittest.main()
