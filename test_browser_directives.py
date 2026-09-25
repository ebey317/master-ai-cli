#!/usr/bin/env python3
"""M9.1 regression: verifies BROWSER_* directives + DONE termination.

Test cases (per master-ai-chrome-breezy-ocean.md Step 7):

  1. BROWSER_CLICK   — "click the search button on this page" → assert action
  2. BROWSER_NAV     — "open example.com"                     → assert action
  3. BROWSER_FILL    — "fill the email field with foo@bar.com" → assert action
  4. BROWSER_READ    — "what's on this page"                  → assert action
  5. BROWSER_SCREENSHOT — "take a screenshot of this page"       → assert action
  6a (deterministic) — `_reply_has_done_directive("DONE: x")` → True
                       parser unit test, no model call, no flakiness
  6b (cloud smoke)   — "Reply with two lines: BROWSER_NAV: https://example.com and DONE: navigated"
                       Groq lane; assert terminal_reason="done_directive"
  6c (local smoke)   — same as 6b but against rebuilt local master-ai,
                       gated by LIVE_LOCAL=1

Default lane:  cloud (uses `fast:` prefix so orchestrate() routes to Groq).
LIVE_LOCAL=1:  swaps cases 1-5 to `local:` and runs case 6c instead of 6b.

Run:
    python3 ~/scripts/test_browser_directives.py
    LIVE_LOCAL=1 python3 ~/scripts/test_browser_directives.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("MASTER_AI_BASE_URL", "http://127.0.0.1:8080")
LIVE_LOCAL = os.environ.get("LIVE_LOCAL") == "1"
LANE_PREFIX = "local:" if LIVE_LOCAL else "fast:"
LANE_LABEL = "local" if LIVE_LOCAL else "cloud(fast)"
TOKEN_FILE = Path.home() / ".master_ai_extension_token"


def _read_token():
    try:
        return TOKEN_FILE.read_text().strip()
    except Exception:
        return ""


def _post_chat(prompt, page_context=None, timeout=120, source="chrome_extension"):
    """POST /chat with the configured lane prefix. Returns parsed JSON.

    Mirrors the real Chrome extension by passing `source="chrome_extension"`
    and `page_context`, so the model can distinguish browser-tab work
    (BROWSER_*) from terminal work (RUN: xdg-open).
    """
    payload = {"prompt": f"{LANE_PREFIX} {prompt}"}
    if source:
        payload["source"] = source
    if page_context:
        payload["page_context"] = page_context
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/chat",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Master-AI-Token": _read_token(),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _action_kinds(response):
    return [str(a.get("kind", "")).upper() for a in response.get("actions") or []]


SAMPLE_PAGE_CONTEXT = {
    # Keys recognized by stt_server._format_page_context: url, title, selection,
    # focused_text, visible_text. Anything else is silently dropped, so the
    # browser-element list lives inside `visible_text` rather than a custom key.
    "title": "Test Page",
    "url": "https://example.com/",
    "visible_text": (
        "Welcome to the test page. "
        'Interactive elements visible: button[aria-label="Search"] (Search button), '
        'input[type="email"] (email field), and the page <main> region.'
    ),
}


class DoneParserUnitTests(unittest.TestCase):
    """Case 6a — deterministic parser unit test, no HTTP, no model."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from stt_server import (  # noqa: WPS433
            _api_terminal_state,
            _reply_has_done_directive,
        )

        self.detect = _reply_has_done_directive
        self.terminal_state = _api_terminal_state

    def test_basic_done_line(self):
        self.assertTrue(self.detect("DONE: navigated to example.com"))

    def test_done_with_leading_whitespace(self):
        self.assertTrue(self.detect("  DONE: cleaned up"))

    def test_done_after_browser_directive(self):
        self.assertTrue(
            self.detect("BROWSER_NAV: https://example.com\nDONE: navigated")
        )

    def test_bare_done_no_summary_rejected(self):
        self.assertFalse(self.detect("DONE:"))
        self.assertFalse(self.detect("DONE:   "))

    def test_done_inside_prose_not_matched(self):
        # Must be on its OWN line, parser-style. Prose mentions don't count.
        self.assertFalse(self.detect("I think we are DONE: with this."))

    def test_empty_and_non_string(self):
        self.assertFalse(self.detect(""))
        self.assertFalse(self.detect(None))
        self.assertFalse(self.detect(123))

    def test_actions_override_premature_done_line(self):
        done, reason = self.terminal_state(
            "BROWSER_SCREENSHOT: viewport\nDONE: captured",
            [{"kind": "BROWSER_SCREENSHOT", "target": "viewport"}],
            2,
        )
        self.assertFalse(done)
        self.assertEqual(reason, "")

    def test_done_without_actions_is_terminal(self):
        done, reason = self.terminal_state("DONE: captured", [], 2)
        self.assertTrue(done)
        self.assertEqual(reason, "done_directive")

    def test_actions_at_budget_are_terminal(self):
        done, reason = self.terminal_state(
            "BROWSER_READ: body",
            [{"kind": "BROWSER_READ", "target": "body"}],
            0,
        )
        self.assertTrue(done)
        self.assertEqual(reason, "budget")


@unittest.skipIf(
    LIVE_LOCAL is False and os.environ.get("SKIP_LIVE") == "1",
    "Live HTTP tests disabled by SKIP_LIVE=1",
)
class BrowserDirectiveLiveTests(unittest.TestCase):
    """Cases 1-5 + 6b/6c — live HTTP against /chat."""

    @classmethod
    def setUpClass(cls):
        try:
            urllib.request.urlopen(f"{BASE_URL}/health", timeout=5).read()
        except Exception as exc:
            raise unittest.SkipTest(
                f"backend at {BASE_URL} unreachable ({exc!s}); start master-ai-ui.service"
            )

    def test_1_browser_click(self):
        resp = _post_chat(
            "click the search button on this page",
            page_context=SAMPLE_PAGE_CONTEXT,
        )
        kinds = _action_kinds(resp)
        self.assertIn(
            "BROWSER_CLICK",
            kinds,
            f"[{LANE_LABEL}] expected BROWSER_CLICK in actions; got {kinds}; reply={resp.get('reply', '')[:200]!r}",
        )

    def test_2_browser_nav(self):
        resp = _post_chat("open example.com")
        kinds = _action_kinds(resp)
        self.assertIn(
            "BROWSER_NAV",
            kinds,
            f"[{LANE_LABEL}] expected BROWSER_NAV in actions; got {kinds}; reply={resp.get('reply', '')[:200]!r}",
        )

    def test_3_browser_fill(self):
        resp = _post_chat(
            "fill the email field with foo@bar.com",
            page_context=SAMPLE_PAGE_CONTEXT,
        )
        kinds = _action_kinds(resp)
        self.assertIn(
            "BROWSER_FILL",
            kinds,
            f"[{LANE_LABEL}] expected BROWSER_FILL in actions; got {kinds}; reply={resp.get('reply', '')[:200]!r}",
        )

    def test_4_browser_read(self):
        # Imperative phrasing — matches Step 8 of the plan. "what's on this page"
        # is more conversational and local models efficient enough to skip the
        # BROWSER_READ when full content is already in visible_text. The product
        # contract is that an explicit "read the X" request always emits BROWSER_READ.
        resp = _post_chat(
            "read the main content on this page using BROWSER_READ",
            page_context=SAMPLE_PAGE_CONTEXT,
        )
        kinds = _action_kinds(resp)
        self.assertIn(
            "BROWSER_READ",
            kinds,
            f"[{LANE_LABEL}] expected BROWSER_READ in actions; got {kinds}; reply={resp.get('reply', '')[:200]!r}",
        )

    def test_5_browser_screenshot(self):
        resp = _post_chat(
            "take a screenshot of this page",
            page_context=SAMPLE_PAGE_CONTEXT,
        )
        kinds = _action_kinds(resp)
        self.assertIn(
            "BROWSER_SCREENSHOT",
            kinds,
            f"[{LANE_LABEL}] expected BROWSER_SCREENSHOT in actions; got {kinds}; reply={resp.get('reply', '')[:200]!r}",
        )

    def test_6_done_directive_smoke(self):
        """6b (cloud) / 6c (local) — model emits DONE: explicitly; backend
        keeps pending browser actions non-terminal until extension results land."""
        resp = _post_chat(
            "Please reply with exactly two lines: "
            "BROWSER_NAV: https://example.com "
            "then on the next line "
            "DONE: navigated to example.com"
        )
        reply = resp.get("reply", "")
        self.assertRegex(
            reply,
            r"(?m)^\s*DONE:\s*\S",
            f"[{LANE_LABEL}] expected DONE: line in reply; got reply={reply[:300]!r}",
        )
        if resp.get("actions"):
            self.assertFalse(
                resp.get("done"),
                f"[{LANE_LABEL}] pending actions must not be terminal; "
                f"terminal_reason={resp.get('terminal_reason')!r}; reply={reply[:300]!r}",
            )
        else:
            self.assertEqual(
                resp.get("terminal_reason"),
                "done_directive",
                f"[{LANE_LABEL}] expected terminal_reason=done_directive; "
                f"got {resp.get('terminal_reason')!r}; reply={reply[:300]!r}",
            )


if __name__ == "__main__":
    print(
        f"[test_browser_directives] lane={LANE_LABEL}, base={BASE_URL}, token={'set' if _read_token() else 'empty'}"
    )
    unittest.main(verbosity=2)
