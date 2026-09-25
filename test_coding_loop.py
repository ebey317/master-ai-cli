#!/usr/bin/env python3
"""Unit tests for the P1.6 coding task loop guardrails.

Two pieces under test:
1. READ-before-EDIT guardrail in process_reply — pins the wiring via
   source inspection (the same pattern test_router_golden uses for the
   RUNTERM blocked-feedback regression guard).
2. Shell syntax-check hook integration via test_hooks.py's coverage —
   not duplicated here.
"""

from __future__ import annotations

import inspect
import os
import sys
import unittest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


class ReadBeforeEditGuardrail(unittest.TestCase):
    def test_process_reply_has_read_before_edit_check(self):
        src = inspect.getsource(master_ai.process_reply)
        # The guard names the repair message explicitly so a refactor
        # that drops it gets caught.
        self.assertIn(
            "READ_BEFORE_EDIT",
            src,
            "process_reply lost the READ→EDIT guardrail (P1.6 regression)",
        )
        self.assertIn(
            "[Directive repair]", src, "Directive repair feedback channel missing"
        )
        # The unread_edits set construction must consult both read_paths
        # and create_files (just-created files are exempt).
        self.assertIn("unread_edits", src)
        self.assertIn("created_set", src)

    def test_guard_returns_none_on_unread_edits(self):
        # The repair branch must use `return None` so the model gets a
        # repair turn (not `return reply` which would advance the chain).
        src = inspect.getsource(master_ai.process_reply)
        idx = src.find("DIRECTIVE_REPAIR_READ_BEFORE_EDIT")
        self.assertGreater(
            idx, 0, "log line for the guard is the anchor — should exist"
        )
        # The next `return None` after that log point should be the guard
        # exit. Crude but effective: take the substring after idx, look
        # for the next return statement.
        after = src[idx : idx + 1500]
        self.assertIn(
            "return None",
            after,
            "READ-before-EDIT guard must return None to trigger repair",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
