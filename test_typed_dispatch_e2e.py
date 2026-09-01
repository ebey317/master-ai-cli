#!/usr/bin/env python3
"""End-to-end tests for the live typed-dispatch boundary (Phase 1.1).

Unlike test_typed_actions.py (schema/parser unit tests, no master_ai
import, no shell), this file verifies the *live* path: that run_command()
and run_in_terminal() -- the single execution choke-points for RUN and
RUNTERM -- actually construct and finalize a real TypedAction on every
invocation, not just a post-hoc shadow parse of raw model text.

run_command tests execute real, harmless commands (true/false) -- that's
the point, this is the live dispatch path, not a mock of it. The timeout
case mocks subprocess.run so the test doesn't actually wait 5 minutes.
run_in_terminal mocks subprocess.Popen so no GUI terminal actually spawns.

Run: python3 ~/scripts/test_typed_dispatch_e2e.py
Exit: 0 = all green, non-zero = at least one live-dispatch typed-record failure.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


class RunCommandTypedLifecycle(unittest.TestCase):
    def setUp(self):
        master_ai._LAST_LIVE_TYPED_ACTIONS.clear()

    def test_successful_command_records_completed_run_action(self):
        master_ai.run_command("true")
        self.assertEqual(len(master_ai._LAST_LIVE_TYPED_ACTIONS), 1)
        action = master_ai._LAST_LIVE_TYPED_ACTIONS[-1]
        self.assertEqual(action["kind"], "RUN")
        self.assertEqual(action["status"], "completed")
        self.assertEqual(action["extras"]["exit_code"], 0)

    def test_failing_command_records_failed_run_action(self):
        master_ai.run_command("false")
        action = master_ai._LAST_LIVE_TYPED_ACTIONS[-1]
        self.assertEqual(action["kind"], "RUN")
        self.assertEqual(action["status"], "failed")
        self.assertEqual(action["extras"]["exit_code"], 1)

    def test_timeout_records_failed_action_with_timeout_marker(self):
        with mock.patch.object(
            master_ai.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="sleep 600", timeout=300),
        ):
            result = master_ai.run_command("sleep 600")
        self.assertFalse(result.ok)
        action = master_ai._LAST_LIVE_TYPED_ACTIONS[-1]
        self.assertEqual(action["kind"], "RUN")
        self.assertEqual(action["status"], "failed")
        self.assertEqual(action["extras"]["error"], "timeout")

    def test_live_typed_actions_bounded(self):
        for _ in range(master_ai._LIVE_TYPED_ACTIONS_CAP + 20):
            master_ai.run_command("true")
        self.assertEqual(
            len(master_ai._LAST_LIVE_TYPED_ACTIONS),
            master_ai._LIVE_TYPED_ACTIONS_CAP,
        )

    def test_audit_jsonl_gets_full_lifecycle_record(self):
        before_size = (
            master_ai.AUDIT_LOG_JSONL.stat().st_size
            if master_ai.AUDIT_LOG_JSONL.exists() else 0
        )
        master_ai.run_command("true")
        with master_ai.AUDIT_LOG_JSONL.open() as f:
            f.seek(before_size)
            new_lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertTrue(new_lines, "expected at least one new JSONL line")
        rec = json.loads(new_lines[-1])
        self.assertEqual(rec["kind"], "RUN")
        self.assertEqual(rec["status"], "completed")
        self.assertIn("id", rec)


class RunInTerminalTypedLifecycle(unittest.TestCase):
    def setUp(self):
        master_ai._LAST_LIVE_TYPED_ACTIONS.clear()

    def test_successful_spawn_records_completed_runterm_action(self):
        with mock.patch.object(master_ai.subprocess, "Popen") as popen:
            popen.return_value = mock.Mock()
            master_ai.run_in_terminal("htop")
        action = master_ai._LAST_LIVE_TYPED_ACTIONS[-1]
        self.assertEqual(action["kind"], "RUNTERM")
        self.assertEqual(action["status"], "completed")
        self.assertIn(action["extras"].get("spawned_via"), (
            "x-terminal-emulator", "gnome-terminal", "xterm",
        ))

    def test_no_terminal_available_records_failed_runterm_action(self):
        with mock.patch.object(master_ai.subprocess, "Popen",
                               side_effect=FileNotFoundError):
            master_ai.run_in_terminal("htop")
        action = master_ai._LAST_LIVE_TYPED_ACTIONS[-1]
        self.assertEqual(action["kind"], "RUNTERM")
        self.assertEqual(action["status"], "failed")


class StandardsCheckReflectsLiveDispatch(unittest.TestCase):
    def test_typed_tool_boundary_check_passes_on_live_probe(self):
        checks = master_ai.agent_standards_checks()
        row = next(c for c in checks if c[1] == "typed tool boundary")
        self.assertEqual(row[0], "PASS")


if __name__ == "__main__":
    unittest.main()
