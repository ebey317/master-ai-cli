#!/usr/bin/env python3
"""Regression tests for explicit-prefix detection inside the [API REQUEST]
envelope (stt_server.py wraps every chrome_extension / pupil prompt).

Before this fix:
  - `_api_prompt()` wraps the user's text in `[API REQUEST] ... [USER PROMPT]\\n<text>`.
  - orchestrate() did `low.startswith("fast:")` against the WHOLE wrapped
    string, which starts with `[api request]`, so the prefix was never
    detected. Execution fell through to link_lookup / other content-based
    short-circuits, and prompts the user explicitly tagged for cloud
    landed on local instead.

After this fix:
  - orchestrate() extracts the user section (after `[USER PROMPT]`) and
    runs prefix matching there.
  - `_strip_prefix(N)` rebuilds the envelope with the prefix removed from
    only the user section, preserving [BROWSER PAGE CONTEXT] etc. for the
    downstream model.

Witnessed 2026-05-14: `fast: take a screenshot of this page` from the
chrome extension on Google search routed to `link_lookup | master-ai`
(local, 9.3s) instead of `cloud_fast | groq`. After the fix, the same
input routes correctly to cloud_fast.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import master_ai

# These tests exercise prefix-routing logic, which is gated on which cloud
# providers have keys configured (see orchestrate()'s have_groq/have_fireworks/
# have_cerebras checks). The real ~/.master_ai_keys is live, mutable, out-of-repo
# credential state — asserting against it makes these tests flaky/environment-
# dependent. Patch load_keys() so routing behavior is deterministic regardless
# of what's actually configured on the machine running the suite.
_FAKE_KEYS = {"groq": "test-groq-key", "fireworks": "test-fireworks-key",
              "cerebras": "test-cerebras-key", "openrouter": "test-or-key"}


def _wrap(user_text: str, page_url: str = "https://www.google.com/") -> str:
    """Mimic stt_server.py _api_prompt() output shape closely enough for
    orchestrate() to behave the same as in production."""
    return (
        "[API REQUEST]\n"
        "source: chrome_extension\n"
        "Branch B: do not execute local machine or browser actions inside the backend request.\n"
        "If browser work is needed, emit BROWSER_CLICK, BROWSER_FILL, BROWSER_READ, BROWSER_NAV, BROWSER_SCREENSHOT, BROWSER_WAIT, BROWSER_SCROLL, BROWSER_DOUBLE_CLICK, BROWSER_FIND, BROWSER_EXTRACT_LIST, or BROWSER_DRIVE_INSPECT_FOLDER directives.\n"
        "\n"
        "[BROWSER PAGE CONTEXT]\n"
        f"url: {page_url}\n"
        "title: jessica rabbit - Google Search\n"
        "visible_text: Skip to main content jessica rabbit ...\n"
        "\n"
        "[USER PROMPT]\n"
        f"{user_text}"
    )


class PrefixInsideEnvelopeTests(unittest.TestCase):
    """All cases assume keys for groq/fireworks/cerebras/openrouter are
    configured (patched via _FAKE_KEYS so results don't depend on the
    live ~/.master_ai_keys file)."""

    def setUp(self):
        patcher = mock.patch.object(master_ai, "load_keys", return_value=_FAKE_KEYS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fast_in_envelope_routes_to_cloud_fast(self):
        wrapped = _wrap("fast: take a screenshot of this page")
        d = master_ai.orchestrate([], wrapped)
        self.assertEqual(d["route"], "cloud_fast")
        self.assertEqual(d["model"], "groq")
        # The envelope head should be preserved; the prefix should be gone
        # from the user section.
        self.assertIn("[BROWSER PAGE CONTEXT]", d["stripped_text"])
        self.assertIn("[USER PROMPT]", d["stripped_text"])
        self.assertIn("take a screenshot of this page", d["stripped_text"])
        self.assertNotIn("fast:", d["stripped_text"].lower())

    def test_deep_in_envelope_routes_to_cloud_deep(self):
        wrapped = _wrap("deep: explain how Cloudflare 1010 errors arise")
        d = master_ai.orchestrate([], wrapped)
        self.assertEqual(d["route"], "cloud_deep")

    def test_local_in_envelope_routes_to_local(self):
        wrapped = _wrap("local: read ~/.bashrc")
        d = master_ai.orchestrate([], wrapped)
        self.assertEqual(d["route"], "local")

    def test_fireworks_prefix_in_envelope(self):
        wrapped = _wrap("fireworks: what's 2+2")
        d = master_ai.orchestrate([], wrapped)
        self.assertEqual(d["route"], "cloud")
        self.assertEqual(d["model"], "fireworks")

    def test_cerebras_prefix_in_envelope(self):
        wrapped = _wrap("cerebras: what's 2+2")
        d = master_ai.orchestrate([], wrapped)
        self.assertEqual(d["route"], "cloud")
        self.assertEqual(d["model"], "cerebras")

    def test_no_prefix_in_envelope_does_not_trigger_prefix_route(self):
        # Without an explicit prefix, the route MAY still land on
        # cloud_fast via content-scored routing (legitimate path). What
        # MUST NOT happen is the prefix detector firing — its reason
        # string is "explicit 'fast:' → Groq" etc., and that string
        # should be absent.
        wrapped = _wrap("take a screenshot of this page")
        d = master_ai.orchestrate([], wrapped)
        reason = str(d.get("reason") or "")
        self.assertNotIn("explicit 'fast:'", reason)
        self.assertNotIn("explicit 'deep:'", reason)
        self.assertNotIn("explicit 'fireworks:'", reason)
        self.assertNotIn("explicit 'cerebras:'", reason)
        self.assertNotIn("explicit local/private", reason)


class PrefixInRawTuiInputTests(unittest.TestCase):
    """Pure TUI input (no API envelope) must still route correctly —
    regression coverage for the unchanged path."""

    def setUp(self):
        patcher = mock.patch.object(master_ai, "load_keys", return_value=_FAKE_KEYS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fast_raw_routes_to_cloud_fast(self):
        d = master_ai.orchestrate([], "fast: what's 2+2")
        self.assertEqual(d["route"], "cloud_fast")
        self.assertEqual(d["model"], "groq")
        self.assertEqual(d["stripped_text"], "what's 2+2")

    def test_local_raw_routes_to_local(self):
        d = master_ai.orchestrate([], "local: read a file")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["stripped_text"], "read a file")

    def test_private_raw_strips_seven_chars(self):
        d = master_ai.orchestrate([], "private: read a file")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["stripped_text"], "read a file")

    def test_no_prefix_raw_falls_through(self):
        # Plain "hello" should not route to any cloud_fast/cloud_deep/local
        # via prefix detection (it might still route via other heuristics).
        d = master_ai.orchestrate([], "hello")
        self.assertNotIn(d["route"], (None, ""))


class EnvelopePreservationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(master_ai, "load_keys", return_value=_FAKE_KEYS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_envelope_head_preserved_when_stripping_fast(self):
        wrapped = _wrap("fast: hello")
        d = master_ai.orchestrate([], wrapped)
        out = d["stripped_text"]
        # Envelope head must still be there
        self.assertIn("[API REQUEST]", out)
        self.assertIn("source: chrome_extension", out)
        self.assertIn("[BROWSER PAGE CONTEXT]", out)
        # User section must be cleaned of the prefix
        tail = out.split("[USER PROMPT]", 1)[1].strip()
        self.assertEqual(tail, "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
