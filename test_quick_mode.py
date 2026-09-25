#!/usr/bin/env python3
"""Tests for Phase 6 Quick Mode.

Backend half: verify _quick_mode_teaching content + that _api_prompt
injects it when mode='quick' and skips it otherwise. Extension half:
delegate to sensei_extension/test/test_quick_mode.js for the parser
assertions.
"""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

srv = pytest.importorskip("stt_server")

JS_TEST = (
    Path(__file__).resolve().parent / "sensei_extension" / "test" / "test_quick_mode.js"
)


class QuickModeTeachingTests(unittest.TestCase):
    def test_teaching_lists_all_seven_commands(self):
        teach = srv._quick_mode_teaching()
        for code in (
            "C x y",
            "T <text>",
            "K <key>",
            "N <url>",
            "J <expr>",
            "W <ms>",
            "ST <tabId>",
        ):
            self.assertIn(code, teach, f"missing command spec: {code}")

    def test_teaching_mentions_end_token(self):
        teach = srv._quick_mode_teaching()
        self.assertIn("<<END>>", teach)

    def test_teaching_warns_off_browser_directives(self):
        teach = srv._quick_mode_teaching()
        self.assertIn("Do NOT emit the BROWSER_* directives in Quick Mode", teach)


class ApiPromptModeTests(unittest.TestCase):
    def test_quick_mode_injects_teaching(self):
        out = srv._api_prompt("hi", source="chrome_extension", mode="quick")
        self.assertIn("QUICK MODE", out)
        self.assertIn("<<END>>", out)

    def test_other_modes_skip_teaching(self):
        for mode in ("review", "plan", "auto", "", "garbage"):
            out = srv._api_prompt("hi", source="chrome_extension", mode=mode)
            self.assertNotIn("QUICK MODE — emit exactly", out)

    def test_quick_mode_still_carries_prompt(self):
        out = srv._api_prompt(
            "the user prompt", source="chrome_extension", mode="quick"
        )
        self.assertIn("the user prompt", out)
        self.assertIn("[USER PROMPT]", out)


class QuickModeParserJsBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not on PATH — Quick Mode JS tests skipped")
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
                "Quick Mode parser assertions failed:\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        self.assertIn("all Quick Mode parser assertions PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
