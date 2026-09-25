#!/usr/bin/env python3
"""Python wrapper for sensei_extension/test/test_cdp_key.js so the CDP
keyboard parser tests run under `python3 -m unittest`."""

import shutil
import subprocess
import unittest
from pathlib import Path

JS_TEST = (
    Path(__file__).resolve().parent / "sensei_extension" / "test" / "test_cdp_key.js"
)


class CdpKeyParserSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not on PATH — CDP key parser tests skipped")
        if not JS_TEST.is_file():
            raise unittest.SkipTest(f"missing test runner at {JS_TEST}")

    def test_all_parser_assertions_pass(self):
        result = subprocess.run(
            ["node", str(JS_TEST)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            self.fail(
                "CDP key parser assertions failed:\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        self.assertIn("all CDP key parser assertions PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
