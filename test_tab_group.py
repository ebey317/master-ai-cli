#!/usr/bin/env python3
"""Python wrapper for sensei_extension/test/test_tab_group.js."""

import shutil
import subprocess
import unittest
from pathlib import Path

JS_TEST = (
    Path(__file__).resolve().parent / "sensei_extension" / "test" / "test_tab_group.js"
)


class TabGroupSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest(
                "node not on PATH — TabGroupManager JS tests skipped"
            )
        if not JS_TEST.is_file():
            raise unittest.SkipTest(f"missing test runner at {JS_TEST}")

    def test_all_assertions_pass(self):
        result = subprocess.run(
            ["node", str(JS_TEST)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            self.fail(
                "TabGroupManager JS assertions failed:\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        self.assertIn("all TabGroupManager assertions PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
