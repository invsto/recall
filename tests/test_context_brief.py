import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "docs/examples/structured-context.md"


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd,
                     "requires symlink-safe file opens")
class ContextBriefTests(unittest.TestCase):
    def run_brief(self, path, home):
        return subprocess.run(
            [sys.executable, "-B", "-m", "recall_core.context_brief", str(path)],
            cwd=ROOT, env={**os.environ, "HOME": str(home)},
            capture_output=True, text=True, timeout=5,
        )

    def test_returns_only_the_quick_read_without_creating_state(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            result = self.run_brief(EXAMPLE, home)
            self.assertEqual(result.returncode, 0, result.stderr)
            brief = json.loads(result.stdout)
            self.assertEqual(set(brief), {"source", "summary", "next_actions"})
            self.assertEqual(brief["source"], EXAMPLE.name)
            self.assertIn("not been independently verified", brief["summary"])
            self.assertIn("Reproduce", brief["next_actions"])
            self.assertNotIn("Supporting detail follows", result.stdout)
            self.assertNotIn("example.test", result.stdout)
            self.assertEqual(list(home.iterdir()), [])

    def test_rejects_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            file_link = home / "context.md"
            file_link.symlink_to(EXAMPLE)
            dir_link = home / "other"
            dir_link.symlink_to(EXAMPLE.parent, target_is_directory=True)
            for path in (file_link, dir_link / EXAMPLE.name):
                with self.subTest(path=path):
                    result = self.run_brief(path, home)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")

    def test_rejects_oversized_briefs_without_partial_output(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            path = home / "large.md"
            document = EXAMPLE.read_text(encoding="utf-8")
            document = document.replace("- Reproduce", "- " + "extra " * 800 + "Reproduce")
            path.write_text(document, encoding="utf-8")
            result = self.run_brief(path, home)
            self.assertEqual(result.returncode, 1)
            self.assertIn("4096-byte brief limit", result.stderr)
            self.assertEqual(result.stdout, "")
