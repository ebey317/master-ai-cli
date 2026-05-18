"""Regression tests for stt_server._api_parse_actions.

Pins the smoking-gun fixed 2026-05-18: a NameError on `chrome_extension`
inside the nested add() function used to cause every chrome_extension-
sourced action to be silently swallowed by the surrounding try/except,
producing actions=[] even when the model emitted a clean directive chain.

Tests assert at least one BROWSER_FILL survives parse for the smoke
payload — pins the bug so the silent-drop pattern can't reappear.
"""

import os
import sys
import unittest
from pathlib import Path

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import stt_server  # noqa: E402


SMOKE_REPLY = """[scratchpad: Fill all form fields from the provided data, then click submit.]

BROWSER_FILL: #firstName :: Elijah
BROWSER_FILL: #lastName :: W.
BROWSER_FILL: #email :: ebey317@gmail.com
BROWSER_FILL: #phone :: 317-555-0100
BROWSER_FILL: #city :: Indianapolis
BROWSER_FILL: #state :: IN
BROWSER_FILL: #zip :: 46201
BROWSER_FILL: #yearsExperience :: 10
BROWSER_CLICK: input[name="workAuth"][value="yes"]
BROWSER_FILL: #coverLetter :: I want this job
BROWSER_WAIT: 300
BROWSER_CLICK: #submitButton"""


SMOKE_PAGE_CONTEXT = {
    "url": "file:///home/elijah/scripts/sensei_extension/test/job_app_smoke.html",
    "title": "Sensei Job App Smoke",
}


class ApiParseActionsRegressionTests(unittest.TestCase):
    def test_at_least_one_browser_fill_survives_chrome_extension_smoke(self):
        """The bug: chrome_extension undefined in _api_parse_actions scope →
        NameError in add() → silent try/except → 0 actions.

        The fix: define chrome_extension at the top of _api_parse_actions
        based on source == "chrome_extension".

        Regression bar: at least one BROWSER_FILL action must survive parse
        for the smoke payload. If this assertion fails again, the silent-
        drop pattern (or a regression of it) has returned.
        """
        actions = stt_server._api_parse_actions(
            SMOKE_REPLY,
            mode="auto",
            source="chrome_extension",
            page_context=SMOKE_PAGE_CONTEXT,
        )
        browser_fills = [a for a in actions if a.get("kind") == "BROWSER_FILL"]
        self.assertGreaterEqual(
            len(browser_fills), 1,
            f"expected ≥1 BROWSER_FILL action to survive parse, got {len(actions)} actions total. "
            f"Silent-drop regression suspect — check stt_server._api_parse_actions for "
            f"NameError-class bugs swallowed by the typed_action try/except."
        )

    def test_smoke_payload_covers_all_11_form_fields(self):
        """Stronger assertion: the smoke payload should parse to actions
        covering every expected smoke-form field. If this loosens (fewer
        than 11 fields), the regression is partial — investigate before
        relaxing the assertion.
        """
        actions = stt_server._api_parse_actions(
            SMOKE_REPLY,
            mode="auto",
            source="chrome_extension",
            page_context=SMOKE_PAGE_CONTEXT,
        )
        expected_anchors = [
            "#firstName", "#lastName", "#email", "#phone", "#city",
            "#state", "#zip", "#yearsExperience", "workAuth",
            "#coverLetter", "#submitButton",
        ]
        covered = set()
        for action in actions:
            target = action.get("target", "")
            for anchor in expected_anchors:
                if anchor in target:
                    covered.add(anchor)
        missing = [a for a in expected_anchors if a not in covered]
        self.assertEqual(
            missing, [],
            f"smoke payload should cover all 11 fields; missing: {missing}. "
            f"Parsed {len(actions)} actions: {[(a.get('kind'), a.get('target')[:40]) for a in actions]}"
        )

    def test_pupil_source_also_works(self):
        """Non-chrome_extension sources must also parse correctly — the fix
        added an explicit chrome_extension boolean, but pupil-sourced
        replies should still produce actions when the directive shape is
        valid.
        """
        actions = stt_server._api_parse_actions(
            "RUN: echo ok\n",
            mode="auto",
            source="pupil",
            page_context=None,
        )
        run_actions = [a for a in actions if a.get("kind") == "RUN"]
        self.assertEqual(
            len(run_actions), 1,
            f"expected exactly one RUN action from pupil source, got {actions!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
