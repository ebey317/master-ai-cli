#!/usr/bin/env python3
"""Regression tests for the BROWSER_SCREENSHOT bare-target default.

Before this fix:
  - `BROWSER_SCREENSHOT:` with no target was silently dropped by both
    _api_parse_actions / _fallback_action in stt_server.py and by
    parse_directive in typed_actions.py. Symptom: Sensei "claims to
    take a snapshot" while no action ever reaches the extension.

After this fix:
  - Bare BROWSER_SCREENSHOT defaults target to "viewport" so the
    directive flows through the parser into the captured-actions list,
    which the side panel auto-runs via captureVisibleTab.

Other directive kinds (RUN, BROWSER_CLICK, BROWSER_FILL, BROWSER_NAV,
BROWSER_READ) still require a non-empty target — no regression there.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stt_server import _api_parse_actions, _fallback_action

from typed_actions import parse_directive, parse_reply


class FallbackActionTests(unittest.TestCase):
    def test_bare_screenshot_defaults_to_viewport(self):
        action = _fallback_action("BROWSER_SCREENSHOT", "", model="cloud_fast")
        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "BROWSER_SCREENSHOT")
        self.assertEqual(action["target"], "viewport")

    def test_explicit_screenshot_target_preserved(self):
        for target in ("viewport", "fullpage"):
            action = _fallback_action("BROWSER_SCREENSHOT", target, model="cloud_fast")
            self.assertIsNotNone(action)
            self.assertEqual(action["target"], target)

    def test_empty_target_still_dropped_for_other_kinds(self):
        for kind in (
            "RUN",
            "BROWSER_CLICK",
            "BROWSER_FILL",
            "BROWSER_NAV",
            "BROWSER_READ",
        ):
            with self.subTest(kind=kind):
                self.assertIsNone(_fallback_action(kind, "", model="cloud_fast"))


class ApiParseActionsTests(unittest.TestCase):
    def test_bare_screenshot_line_yields_action(self):
        reply = "I will capture the visible tab.\nBROWSER_SCREENSHOT:"
        actions = _api_parse_actions(
            reply, model="cloud_fast", source="chrome_extension"
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "BROWSER_SCREENSHOT")
        self.assertEqual(actions[0]["target"], "viewport")

    def test_screenshot_with_viewport_unchanged(self):
        actions = _api_parse_actions("BROWSER_SCREENSHOT: viewport", model="cloud_fast")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["target"], "viewport")

    def test_screenshot_with_fullpage_unchanged(self):
        actions = _api_parse_actions("BROWSER_SCREENSHOT: fullpage", model="cloud_fast")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["target"], "fullpage")

    def test_bare_click_still_dropped(self):
        actions = _api_parse_actions("BROWSER_CLICK:", model="cloud_fast")
        self.assertEqual(actions, [])

    def test_bare_fill_still_dropped(self):
        actions = _api_parse_actions("BROWSER_FILL:", model="cloud_fast")
        self.assertEqual(actions, [])

    def test_bare_nav_still_dropped(self):
        actions = _api_parse_actions("BROWSER_NAV:", model="cloud_fast")
        self.assertEqual(actions, [])

    def test_prose_without_directive_still_yields_nothing(self):
        reply = "I took a screenshot of this page for you."
        self.assertEqual(_api_parse_actions(reply, model="cloud_fast"), [])

    def test_drive_inspect_line_yields_action(self):
        reply = 'BROWSER_DRIVE_INSPECT_FOLDER: {"query":"resume","variants":["Resume","resume"]}'
        actions = _api_parse_actions(
            reply, model="cloud_fast", source="chrome_extension", mode="auto"
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "BROWSER_DRIVE_INSPECT_FOLDER")
        self.assertEqual(actions[0]["risk"], "safe")
        self.assertFalse(actions[0]["requires_confirm"])

    def test_new_browser_observation_actions_parse(self):
        reply = "\n".join(
            [
                "BROWSER_WAIT: 2000",
                "BROWSER_SCROLL: down",
                "BROWSER_FIND: Resume",
                "BROWSER_EXTRACT_LIST: drive",
                "BROWSER_DOUBLE_CLICK: [aria-label='Resume']",
            ]
        )
        actions = _api_parse_actions(
            reply, model="cloud_fast", source="chrome_extension", mode="auto"
        )
        self.assertEqual(
            [a["kind"] for a in actions],
            [
                "BROWSER_WAIT",
                "BROWSER_SCROLL",
                "BROWSER_FIND",
                "BROWSER_EXTRACT_LIST",
                "BROWSER_DOUBLE_CLICK",
            ],
        )

    def test_indented_bare_screenshot_yields_action(self):
        reply = "  BROWSER_SCREENSHOT:"
        actions = _api_parse_actions(reply, model="cloud_fast")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["target"], "viewport")


class TypedActionsTests(unittest.TestCase):
    def test_parse_directive_bare_screenshot(self):
        action = parse_directive("BROWSER_SCREENSHOT:", model="cloud_fast")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "BROWSER_SCREENSHOT")
        self.assertEqual(action.target, "viewport")

    def test_parse_directive_bare_click_still_returns_none(self):
        self.assertIsNone(parse_directive("BROWSER_CLICK:", model="cloud_fast"))

    def test_parse_reply_picks_up_bare_screenshot(self):
        text = "Taking a screenshot now.\nBROWSER_SCREENSHOT:"
        actions = parse_reply(text, model="cloud_fast")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "BROWSER_SCREENSHOT")
        self.assertEqual(actions[0].target, "viewport")


if __name__ == "__main__":
    unittest.main(verbosity=2)
