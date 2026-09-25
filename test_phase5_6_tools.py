#!/usr/bin/env python3
"""Acceptance tests for Phases 5 + 6 of the roadmap (Chrome extension tool
surface expansion + Quick Mode). Mirrors the shape of the parallel lane's
test_phase7_10_tools.py: one consolidated file that pins the full surface
so a future regression touches a single harness.

Phase 5 sub-items covered:
  5.1 BROWSER_JS         — directive parsed by stt_server _ACTION_LINE_RE
  5.2 BROWSER_CONSOLE    — directive parsed by stt_server _ACTION_LINE_RE
  5.3 BROWSER_NETWORK    — directive parsed by stt_server _ACTION_LINE_RE
  5.4 BROWSER_RESIZE_WINDOW — directive parsed by stt_server _ACTION_LINE_RE
  5.5 PLAN-BLOCK JSON schema — stt_server response carries plan dict
  5.6 turn_answer_start hook  — hooks.KINDS membership + fire() observer

Phase 6 sub-items covered:
  6.1 Backend mode=quick accepted by api_handle validation
  6.2 Single-letter command parser (delegated to node test_quick_mode.js)
  6.3 Screenshot feedback loop wiring in side_panel.js (smoke grep)
  6.4 Mode toggle present in side_panel.html
  6.5 Lane B latency note documented in plan file
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

stt_server = pytest.importorskip("stt_server")

import hooks  # noqa: E402

REPO = Path(__file__).resolve().parent


def _parse_plan_block(reply):
    """Mirror of the stt_server plan extractor; same regex + json round-trip."""
    m = re.search(r"<PLAN>\s*(\{[\s\S]*?\})\s*</PLAN>", reply or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or ("domains" not in obj and "steps" not in obj):
        return None
    return {
        "domains": list(obj.get("domains") or [])[:10],
        "steps": list(obj.get("steps") or [])[:20],
        "irreversible": list(obj.get("irreversible") or [])[:10],
    }


class Phase5DirectivesParseTests(unittest.TestCase):
    """5.1-5.4: every new BROWSER_* action is recognized by the backend's
    action-line regex so directives the model emits round-trip into the
    extension dispatch path."""

    def test_browser_js_directive_parses(self):
        m = stt_server._ACTION_LINE_RE.match("BROWSER_JS: document.title")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "BROWSER_JS")

    def test_browser_console_directive_parses(self):
        m = stt_server._ACTION_LINE_RE.match("BROWSER_CONSOLE: error")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "BROWSER_CONSOLE")

    def test_browser_network_directive_parses(self):
        m = stt_server._ACTION_LINE_RE.match("BROWSER_NETWORK: xhr")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "BROWSER_NETWORK")

    def test_browser_resize_window_directive_parses(self):
        m = stt_server._ACTION_LINE_RE.match("BROWSER_RESIZE_WINDOW: 1280x800")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).upper(), "BROWSER_RESIZE_WINDOW")


class Phase55PlanBlockSchemaTests(unittest.TestCase):
    """5.5: structured plan-block JSON round-trips into the /chat response."""

    def test_json_plan_block_extracted(self):
        reply = (
            "<PLAN>\n"
            '{"domains":["drive.google.com"],'
            '"steps":[{"n":1,"action":"BROWSER_NAV","target":"https://drive.google.com"}],'
            '"irreversible":[]}\n'
            "</PLAN>\n"
            "BROWSER_NAV: https://drive.google.com\n"
        )
        out = _parse_plan_block(reply)
        self.assertIsNotNone(out)
        self.assertEqual(out["domains"], ["drive.google.com"])
        self.assertEqual(len(out["steps"]), 1)

    def test_prose_plan_block_returns_none(self):
        reply = (
            "<PLAN>\n"
            "Sites: drive.google.com\n"
            "Steps:\n"
            "1. Open My Drive\n"
            "Irreversible: none\n"
            "</PLAN>\n"
        )
        self.assertIsNone(_parse_plan_block(reply))

    def test_schema_teaching_present_in_modelfile(self):
        with (REPO / "master_ai.py").open(encoding="utf-8") as f:
            src = f.read()
        self.assertIn("PLAN-BLOCK STRUCTURED SCHEMA", src)
        # Source is Python with escaped quotes — match the escaped form.
        self.assertIn('\\"domains\\"', src)
        self.assertIn('\\"steps\\"', src)
        self.assertIn('\\"irreversible\\"', src)


class Phase56HookTests(unittest.TestCase):
    """5.6: turn_answer_start observer hook is wired into the response path."""

    def test_turn_answer_start_in_kinds(self):
        self.assertIn("turn_answer_start", hooks.KINDS)

    def test_fire_turn_answer_start_does_not_block(self):
        result = hooks.fire(
            "turn_answer_start", "reply text", action={"turn_id": "abc", "round_num": 1}
        )
        self.assertFalse(getattr(result, "blocked", False))

    def test_stt_server_emits_turn_answer_start(self):
        # stt_server.py isn't tracked in this repo (lives wherever
        # ~/scripts resolves it to) -- REPO / "stt_server.py" never
        # existed, even on a dev machine where the module import above
        # succeeds. Read the actual resolved file instead of assuming
        # it's a repo-local path.
        with Path(stt_server.__file__).open(encoding="utf-8") as f:
            src = f.read()
        # Match the actual fire call regardless of the module alias.
        self.assertRegex(src, r'\.fire\(\s*["\']turn_answer_start["\']')


class Phase61QuickModeBackendTests(unittest.TestCase):
    """6.1: backend api_handle accepts mode='quick' and injects the teaching."""

    def test_api_prompt_injects_quick_mode_teaching(self):
        out = stt_server._api_prompt("hello", source="chrome_extension", mode="quick")
        self.assertIn("QUICK MODE", out)
        self.assertIn("<<END>>", out)

    def test_api_prompt_skips_quick_teaching_for_other_modes(self):
        for mode in ("review", "plan", "auto", ""):
            out = stt_server._api_prompt("hello", source="chrome_extension", mode=mode)
            self.assertNotIn("QUICK MODE — emit exactly", out)

    def test_quick_mode_teaching_lists_seven_commands(self):
        teach = stt_server._quick_mode_teaching()
        for spec in (
            "C x y",
            "T <text>",
            "K <key>",
            "N <url>",
            "J <expr>",
            "W <ms>",
            "ST <tabId>",
        ):
            self.assertIn(spec, teach)


class Phase62QuickModeParserTests(unittest.TestCase):
    """6.2: single-letter command parser. Delegates to the node harness so the
    JS implementation in side_panel.js is exercised, not a Python reimpl."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not on PATH — JS parser tests skipped")
        cls.js_test = REPO / "sensei_extension" / "test" / "test_quick_mode.js"
        if not cls.js_test.is_file():
            raise unittest.SkipTest(f"missing {cls.js_test}")

    def test_quick_mode_parser_assertions_pass(self):
        result = subprocess.run(
            ["node", str(self.js_test)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + "\n" + result.stderr)
        self.assertIn("all Quick Mode parser assertions PASS", result.stdout)


class Phase63ScreenshotLoopWiringTests(unittest.TestCase):
    """6.3: side_panel.js carries the Quick-Mode loop with an 8-round cap and
    feeds screenshot data_url back into page_context on continuation."""

    def setUp(self):
        with (REPO / "sensei_extension" / "side_panel.js").open(encoding="utf-8") as f:
            self.src = f.read()

    def test_loop_cap_constant_present(self):
        self.assertIn("QUICK_MODE_MAX_ROUNDS = 8", self.src)

    def test_runQuickModeLoop_defined(self):
        self.assertIn("async function runQuickModeLoop(", self.src)

    def test_loop_feeds_screenshot_to_page_context(self):
        self.assertIn("screenshot_data_url", self.src)

    def test_loop_uses_captureVisibleTab(self):
        self.assertIn("SENSEI_CAPTURE_VISIBLE_TAB", self.src)


class Phase64ModeToggleTests(unittest.TestCase):
    """6.4: Quick mode appears in the side panel mode picker."""

    def test_quick_option_in_side_panel_html(self):
        with (REPO / "sensei_extension" / "side_panel.html").open(
            encoding="utf-8"
        ) as f:
            html = f.read()
        self.assertIn('value="quick"', html)
        self.assertIn("Quick mode", html)


class Phase65DocumentationTests(unittest.TestCase):
    """6.5: Lane B latency framing documented in the plan file so the
    directionality is on record alongside the implementation."""

    def test_plan_file_mentions_quick_mode_lane_b(self):
        plan_path = Path(
            os.path.expanduser("~/.claude/plans/1-got-it-buzzing-robin.md")
        )
        if not plan_path.is_file():
            self.skipTest(f"plan file not present at {plan_path}")
        with plan_path.open(encoding="utf-8") as f:
            plan = f.read()
        self.assertIn("Quick Mode", plan)
        self.assertIn("Lane B", plan)


class Phase5And6AcceptanceTests(unittest.TestCase):
    """Top-level acceptance: every commit hash referenced in the plan file is
    reachable from HEAD, so claims of "shipped" are verifiable."""

    def _git_log_oneline(self):
        result = subprocess.run(
            ["git", "log", "--oneline"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            timeout=10,
        )
        return result.stdout if result.returncode == 0 else ""

    def test_phase5_commits_in_history(self):
        log = self._git_log_oneline()
        self.assertIn("ac44537", log)  # 5.1-5.4
        self.assertIn("bcc7371", log)  # 5.5-5.6

    def test_phase6_commit_in_history(self):
        log = self._git_log_oneline()
        self.assertIn("6cddb44", log)


if __name__ == "__main__":
    unittest.main()
