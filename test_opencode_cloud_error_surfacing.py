#!/usr/bin/env python3
"""2026-10-25: the cloud-failure reason used to die in log() only -- the
chat UI never saw more than a generic "maxed out, rate-limited, or
erroring" guess no matter what actually happened. Elijah: "tell me why my
opencode key silently fails... I don't want to always have to depend on
my backup." These pin that _LAST_CLOUD_ERROR carries the real reason from
_ask_opencode_go's actual failure points through to the printed notice,
and that it never leaks into an unrelated later notice.

Mirrors test_master_ai_safety.py's monkeypatch style -- no real network
calls, no shell commands.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.dirname(__file__))

import master_ai  # noqa: E402


def _silence_log():
    master_ai.log = lambda *a, **k: None


class TestOpenCodeGoErrorSurfacing(unittest.TestCase):
    def setUp(self):
        _silence_log()
        master_ai.__dict__.pop("_LAST_CLOUD_ERROR", None)
        self._orig_cloud_allowed = master_ai._cloud_allowed
        self._orig_key_fn = master_ai._opencode_go_key
        self._orig_circuits = dict(master_ai._ROUTING_CLOUD_CIRCUITS)

    def tearDown(self):
        master_ai._cloud_allowed = self._orig_cloud_allowed
        master_ai._opencode_go_key = self._orig_key_fn
        master_ai._ROUTING_CLOUD_CIRCUITS.clear()
        master_ai._ROUTING_CLOUD_CIRCUITS.update(self._orig_circuits)
        master_ai.__dict__.pop("_LAST_CLOUD_ERROR", None)

    def test_missing_key_sets_specific_reason(self):
        master_ai._cloud_allowed = lambda *_: True
        master_ai._opencode_go_key = lambda: ""
        result = master_ai._ask_opencode_go(
            [{"role": "user", "content": "hi"}], "m", "space-bunny"
        )
        self.assertIsNone(result)
        reason = master_ai.__dict__.get("_LAST_CLOUD_ERROR")
        self.assertIsNotNone(
            reason, "a missing key must leave a reason, not just a log line"
        )
        self.assertIn("space-bunny", reason)
        self.assertIn("OPENCODE_API_KEY", reason)

    def test_open_circuit_sets_cooldown_reason(self):
        master_ai._opencode_go_key = lambda: "real-looking-key"
        master_ai._ROUTING_CLOUD_CIRCUITS["opencode-go/space-bunny"] = (
            master_ai.time.time() + 17
        )
        result = master_ai._ask_opencode_go(
            [{"role": "user", "content": "hi"}], "m", "space-bunny"
        )
        self.assertIsNone(result)
        reason = master_ai.__dict__.get("_LAST_CLOUD_ERROR")
        self.assertIsNotNone(reason)
        self.assertIn("cooldown", reason)

    def test_notice_surfaces_and_then_clears_the_reason(self):
        master_ai.__dict__["_LAST_CLOUD_ERROR"] = (
            "opencode-go/space-bunny: HTTP 401 — invalid api key"
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            master_ai._notice_pinned_falling_back(
                "opencode-go::space-bunny", ["opencode"]
            )
        printed = buf.getvalue()
        self.assertIn(
            "HTTP 401", printed, "the real reason must reach the printed notice"
        )
        self.assertIn("invalid api key", printed)
        self.assertIsNone(
            master_ai.__dict__.get("_LAST_CLOUD_ERROR"),
            "a consumed reason must not leak into the next, unrelated notice",
        )

    def test_notice_falls_back_to_generic_text_when_no_reason_recorded(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            master_ai._notice_pinned_falling_back(
                "opencode-go::space-bunny", ["opencode"]
            )
        printed = buf.getvalue()
        self.assertIn("maxed out, rate-limited, or erroring", printed)


if __name__ == "__main__":
    unittest.main()
