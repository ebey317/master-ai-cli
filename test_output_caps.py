#!/usr/bin/env python3
"""Unit tests for P2.3 / Phase 2 output caps.

Verifies that the RUN command path caps oversized tool output and that
the standards check 'output caps' reports PASS.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


class OutputCapsTest(unittest.TestCase):
    def _require_sandbox(self):
        """2026-10-14: the cap tests below exercise the real RUN path,
        whose sandbox wrapper needs systemd-run + unshare. GitHub runners
        have neither, so the command returns not-ok there for environment
        reasons; skip in that case instead of misreporting a broken cap.
        (test_output_caps_standards_pass is static and always runs.)"""
        try:
            ok = bool(master_ai.run_command("true").ok)
        except Exception:
            ok = False
        if not ok:
            self.skipTest("RUN sandbox not functional in this environment")

    def test_short_output_not_truncated(self):
        self._require_sandbox()
        result = master_ai.run_command("echo hi")
        self.assertTrue(master_ai._action_ok(result))
        self.assertIn("hi", str(result))

    def test_long_output_is_capped(self):
        self._require_sandbox()
        # Generate 50k chars and verify the formatted result is truncated.
        big = "x" * 50000
        result = master_ai.run_command(f"printf '%s' '{big}'")
        formatted = master_ai._format_tool_result("RUN", "printf big", result)
        self.assertIn("truncated", formatted.lower())
        self.assertNotIn(big, formatted)
        body_start = formatted.find("Output:\n") + len("Output:\n")
        body = formatted[body_start:]
        self.assertLessEqual(
            len(body),
            13000,
            f"truncated body should be near 12000 chars, got {len(body)}",
        )

    def test_empty_output_replaced(self):
        self._require_sandbox()
        result = master_ai.run_command("true")
        formatted = master_ai._format_tool_result("RUN", "true", result)
        self.assertIn("[no output]", formatted)

    def test_output_caps_standards_pass(self):
        checks = master_ai.agent_standards_checks()
        oc = next((c for c in checks if c[1] == "output caps"), None)
        self.assertIsNotNone(oc, "output caps check missing")
        self.assertEqual(oc[0], "PASS", f"output caps should be PASS: {oc}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
