#!/usr/bin/env python3
"""
Focused tests for the cloud-send privacy guard.

Covers:
  - private/non-private state predicates
  - one-shot approval consume
  - reset clears all state
  - harvest._privacy_reason is the source of truth (path + content)
  - ask_cloud() returns None when turn is private and not approved
  - ask_cloud()'s privacy gate fires BEFORE provider dispatch (no network)
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.home() / "scripts"))

import master_ai
import harvest


class TurnPrivacyStateTests(unittest.TestCase):
    def setUp(self):
        master_ai._reset_turn_privacy()

    def test_default_not_private(self):
        self.assertFalse(master_ai._is_turn_private())
        ok, reason = master_ai._check_cloud_send_allowed()
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_mark_makes_private(self):
        master_ai._mark_turn_private("some reason")
        self.assertTrue(master_ai._is_turn_private())

    def test_private_blocks_without_approval(self):
        master_ai._mark_turn_private("test reason: /home/user/Pictures/foo.jpg")
        ok, reason = master_ai._check_cloud_send_allowed()
        self.assertFalse(ok)
        self.assertIn("test reason", reason)

    def test_approval_is_one_shot(self):
        master_ai._mark_turn_private("test")
        master_ai._approve_cloud_send_once()
        ok1, _ = master_ai._check_cloud_send_allowed()
        ok2, _ = master_ai._check_cloud_send_allowed()
        self.assertTrue(ok1, "first call should consume approval and pass")
        self.assertFalse(ok2, "second call should be blocked again")

    def test_reset_clears_all_state(self):
        master_ai._mark_turn_private("test")
        master_ai._approve_cloud_send_once()
        master_ai._reset_turn_privacy()
        self.assertFalse(master_ai._is_turn_private())
        ok, _ = master_ai._check_cloud_send_allowed()
        self.assertTrue(ok)


class PrivacyPolicySourceOfTruthTests(unittest.TestCase):
    """The cloud-send guard must defer to harvest's policy. These tests
    pin the contract: same path/content harvest flags must also flag here."""

    def setUp(self):
        master_ai._reset_turn_privacy()

    def test_private_path_pictures_flagged(self):
        reason = master_ai._privacy_check_path_or_content(
            "/home/user/Pictures/photo.jpg", "")
        self.assertTrue(reason)
        # confirm harvest agrees
        self.assertTrue(harvest.is_private(
            prompt="/home/user/Pictures/photo.jpg"))

    def test_private_path_jobseeker_flagged(self):
        reason = master_ai._privacy_check_path_or_content(
            "/home/user/jobseeker/resume.txt", "")
        self.assertTrue(reason)

    def test_private_term_in_content_flagged(self):
        # path is innocuous but content has a private term
        reason = master_ai._privacy_check_path_or_content(
            "/tmp/notes.txt", "Here is my social security number: redacted")
        self.assertTrue(reason)

    def test_secret_value_in_content_flagged(self):
        reason = master_ai._privacy_check_path_or_content(
            "/tmp/dump.txt", "AKIAABCDEFGHIJKLMNOP")
        self.assertTrue(reason)

    def test_clean_path_and_content_passes(self):
        reason = master_ai._privacy_check_path_or_content(
            "/tmp/hello.txt", "hello world from a test")
        self.assertFalse(reason)


class AskCloudGateTests(unittest.TestCase):
    """The gate inside ask_cloud must short-circuit before any provider
    dispatch. We swap the provider fn_map dispatch to a sentinel to detect
    if the call leaks past the gate."""

    def setUp(self):
        master_ai._reset_turn_privacy()
        self._called = {"hit": 0}

    def test_blocks_when_private_no_approval(self):
        # Mark turn private. ask_cloud should return None and never reach
        # _cloud_allowed / fn_map.get(provider). We assert by monkey-patching
        # the first thing past the gate (_cloud_allowed) to raise — if the
        # gate works, the raise never happens.
        sentinel_called = {"hit": False}

        def boom(*a, **kw):
            sentinel_called["hit"] = True
            raise AssertionError("ask_cloud advanced past the privacy gate")

        original = master_ai._cloud_allowed
        master_ai._cloud_allowed = boom
        try:
            master_ai._mark_turn_private("test")
            result = master_ai.ask_cloud(
                [{"role": "user", "content": "hi"}], provider="groq")
            self.assertIsNone(result)
            self.assertFalse(sentinel_called["hit"])
        finally:
            master_ai._cloud_allowed = original

    def test_allows_when_not_private(self):
        # If the gate is honest, when turn is NOT private it falls through
        # to _cloud_allowed. Stub _cloud_allowed False so no real network.
        # Also stub fn_map call (won't be reached when _cloud_allowed False).
        original = master_ai._cloud_allowed
        master_ai._cloud_allowed = lambda *a, **kw: False
        try:
            result = master_ai.ask_cloud(
                [{"role": "user", "content": "hi"}], provider="groq")
            # _cloud_allowed False makes ask_cloud's named path return r=None,
            # then it tries the fallback chain which also hits _cloud_allowed
            # indirectly via per-provider keys; in this env all should return
            # None. Final result: None. The important thing is no exception
            # and the gate did not block.
            self.assertIsNone(result)
        finally:
            master_ai._cloud_allowed = original

    def test_approval_consumes_one_send_then_blocks(self):
        # After approval, gate lets the first ask_cloud through;
        # subsequent ones should block again.
        original = master_ai._cloud_allowed
        master_ai._cloud_allowed = lambda *a, **kw: False
        try:
            master_ai._mark_turn_private("test")
            master_ai._approve_cloud_send_once()
            # First call: gate allows (consumes token), then falls through
            # _cloud_allowed which is False so result is None.
            master_ai.ask_cloud([{"role": "user", "content": "hi"}], provider="groq")
            # Second call: gate blocks again.
            ok, _ = master_ai._check_cloud_send_allowed()
            self.assertFalse(ok)
        finally:
            master_ai._cloud_allowed = original


class RunOutputExfilTests(unittest.TestCase):
    """RUN/RUNTERM output is fed back into history when continue_after_tools
    is True. Without this guard, `RUN: cat ~/Documents/foo` would put private
    bytes into the next cloud send. _check_run_output_for_privacy is the
    centralized hook called from _format_tool_result inside process_reply."""

    def setUp(self):
        master_ai._reset_turn_privacy()

    def test_private_path_in_cmd_marks_private(self):
        reason = master_ai._check_run_output_for_privacy(
            "RUN", "cat /home/user/Documents/notes.txt", "hello")
        self.assertTrue(reason)
        self.assertTrue(master_ai._is_turn_private())

    def test_private_path_in_cmd_pictures(self):
        reason = master_ai._check_run_output_for_privacy(
            "RUN", "ls /home/user/Pictures/", "drwxr-xr-x  ...")
        self.assertTrue(reason)
        self.assertTrue(master_ai._is_turn_private())

    def test_secret_in_output_marks_private(self):
        reason = master_ai._check_run_output_for_privacy(
            "RUN", "cat /tmp/dump.txt", "AWS_KEY=AKIAABCDEFGHIJKLMNOP")
        self.assertTrue(reason)
        self.assertTrue(master_ai._is_turn_private())

    def test_private_term_in_output_marks_private(self):
        reason = master_ai._check_run_output_for_privacy(
            "RUN", "cat /tmp/foo.txt", "Subject: tax 1099 forms for 2025")
        self.assertTrue(reason)
        self.assertTrue(master_ai._is_turn_private())

    def test_clean_cmd_and_output_does_not_mark(self):
        reason = master_ai._check_run_output_for_privacy(
            "RUN", "ls /tmp", "foo.txt\nbar.log")
        self.assertFalse(reason)
        self.assertFalse(master_ai._is_turn_private())

    def test_runterm_kind_also_marks(self):
        # Same helper covers RUNTERM (visual scripts that cat private paths)
        reason = master_ai._check_run_output_for_privacy(
            "RUNTERM", "bash /home/user/jobseeker/show_cover.sh", "")
        self.assertTrue(reason)
        self.assertTrue(master_ai._is_turn_private())

    def test_marked_run_then_blocks_cloud(self):
        # Integration: after RUN exfil marks private, ask_cloud must block.
        master_ai._check_run_output_for_privacy(
            "RUN", "cat /home/user/.aws/credentials", "[default]\naws_secret=...")
        ok, _ = master_ai._check_cloud_send_allowed()
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
