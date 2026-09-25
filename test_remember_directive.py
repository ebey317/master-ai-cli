#!/usr/bin/env python3
"""Unit tests for the REMEMBER: directive (self-write to memory).

The user's `remember:` REPL command has always written to MEMORY_FILE;
this gives the model the same path via REMEMBER: <fact> in its
directive stream. Tests verify:

- REMEMBER: <fact> appends to MEMORY_FILE via confirm_remember()
- Empty / whitespace-only payloads are skipped
- Duplicate lines are detected and skipped
- 200-char cap is enforced
- Multiple REMEMBER lines in one reply all fire
- Directive isn't matched inside backticks (parser parity check)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


class ConfirmRememberDirect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        self.tmp.close()
        self.path = Path(self.tmp.name)
        self._orig_file = master_ai.MEMORY_FILE
        master_ai.MEMORY_FILE = self.path

    def tearDown(self):
        master_ai.MEMORY_FILE = self._orig_file
        try:
            self.path.unlink()
        except Exception:
            pass

    def _mem(self):
        return [l for l in self.path.read_text().splitlines() if l.strip()]

    def test_appends_simple_fact(self):
        self.assertTrue(
            master_ai.confirm_remember("user prefers Thunderbird for email")
        )
        self.assertEqual(self._mem(), ["user prefers Thunderbird for email"])

    def test_empty_returns_false(self):
        self.assertFalse(master_ai.confirm_remember(""))
        self.assertFalse(master_ai.confirm_remember("   "))
        self.assertFalse(master_ai.confirm_remember(None))
        self.assertEqual(self._mem(), [])

    def test_duplicate_skipped(self):
        self.assertTrue(
            master_ai.confirm_remember("never spawn terminal for thunderbird")
        )
        self.assertFalse(
            master_ai.confirm_remember("never spawn terminal for thunderbird")
        )
        self.assertEqual(len(self._mem()), 1)

    def test_long_fact_truncated(self):
        long_fact = "x" * 500
        self.assertTrue(master_ai.confirm_remember(long_fact))
        stored = self._mem()[0]
        self.assertLessEqual(len(stored), 203)  # 200 chars + "..."
        self.assertTrue(stored.endswith("..."))

    def test_strips_double_remember_prefix(self):
        # Model accidentally wrapping the line — strip it.
        self.assertTrue(master_ai.confirm_remember("REMEMBER: actually a clean fact"))
        self.assertEqual(self._mem(), ["actually a clean fact"])


class ProcessReplyExtractsRemember(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        self.tmp.close()
        self.path = Path(self.tmp.name)
        self._orig_file = master_ai.MEMORY_FILE
        master_ai.MEMORY_FILE = self.path
        # Silence pills + audit
        self._orig_pill = master_ai._pill
        self._orig_audit = master_ai._audit
        self._orig_log = master_ai.log
        master_ai._pill = lambda label, msg="": ""
        master_ai._audit = lambda *a, **k: None
        master_ai.log = lambda *a, **k: None

    def tearDown(self):
        master_ai.MEMORY_FILE = self._orig_file
        master_ai._pill = self._orig_pill
        master_ai._audit = self._orig_audit
        master_ai.log = self._orig_log
        try:
            self.path.unlink()
        except Exception:
            pass

    def _mem(self):
        return [l for l in self.path.read_text().splitlines() if l.strip()]

    def test_single_remember_line_fires(self):
        master_ai.process_reply(
            "REMEMBER: when user says 'open my email', open Thunderbird via desktop launcher",
            [],
            streamed=False,
        )
        mem = self._mem()
        self.assertEqual(len(mem), 1)
        self.assertIn("Thunderbird", mem[0])

    def test_multiple_remember_lines_all_fire(self):
        master_ai.process_reply(
            "Here's what I learned:\n"
            "REMEMBER: first lesson\n"
            "REMEMBER: second lesson\n"
            "REMEMBER: third lesson",
            [],
            streamed=False,
        )
        mem = self._mem()
        self.assertEqual(len(mem), 3)
        self.assertIn("first lesson", mem)
        self.assertIn("second lesson", mem)
        self.assertIn("third lesson", mem)

    def test_remember_inside_create_body_does_NOT_fire(self):
        """REMEMBER lines inside <<<CONTENT>>>CONTENT are document
        content, not directives. Pre-fix the parser would write them to
        memory silently."""
        orig_create = master_ai.confirm_create
        master_ai.confirm_create = lambda p, c: True
        try:
            master_ai.process_reply(
                "CREATE: /tmp/notes-test.md\n"
                "<<<CONTENT\n"
                "REMEMBER: this is example documentation only\n"
                "REMEMBER: another example line\n"
                ">>>CONTENT",
                [],
                streamed=False,
            )
        finally:
            master_ai.confirm_create = orig_create
        self.assertEqual(
            self._mem(),
            [],
            "REMEMBER inside <<<CONTENT>>>CONTENT must not fire — "
            "those are document lines, not directives",
        )

    def test_remember_inside_find_replace_body_does_NOT_fire(self):
        """Same protection for <<<FIND>>>FIND and <<<REPLACE>>>REPLACE."""
        orig_edit = master_ai.confirm_edit
        master_ai.confirm_edit = lambda p, f, r: True
        # Need a READ before EDIT (P1.6 contract)
        try:
            master_ai.process_reply(
                "READ: /tmp/notes-test.md\n"
                "EDIT: /tmp/notes-test.md\n"
                "<<<FIND\n"
                "REMEMBER: don't catch this\n"
                ">>>FIND\n"
                "<<<REPLACE\n"
                "REMEMBER: don't catch this either\n"
                ">>>REPLACE",
                [],
                streamed=False,
            )
        finally:
            master_ai.confirm_edit = orig_edit
        self.assertEqual(
            self._mem(), [], "REMEMBER inside <<<FIND/REPLACE>>> blocks must not fire"
        )

    def test_remember_AFTER_create_block_still_fires(self):
        """The block-state filter resets at the >>>CONTENT marker, so a
        REMEMBER line that comes AFTER the close marker should still
        fire. Pin this so the fix doesn't over-suppress."""
        orig_create = master_ai.confirm_create
        master_ai.confirm_create = lambda p, c: True
        try:
            master_ai.process_reply(
                "CREATE: /tmp/notes-test.md\n"
                "<<<CONTENT\n"
                "REMEMBER: inside the body — ignored\n"
                ">>>CONTENT\n"
                "REMEMBER: outside the body — should fire",
                [],
                streamed=False,
            )
        finally:
            master_ai.confirm_create = orig_create
        self.assertEqual(self._mem(), ["outside the body — should fire"])

    def test_backtick_wrapped_remember_does_not_fire(self):
        # `REMEMBER:` inside backticks is prose, not a directive.
        # _real_directive enforces the backtick-parity rule.
        master_ai.process_reply(
            "I can write to memory via `REMEMBER:` syntax.",
            [],
            streamed=False,
        )
        self.assertEqual(self._mem(), [])

    def test_lowercase_remember_directive(self):
        # The directive parser is case-insensitive (matches RUN/run/Run pattern).
        master_ai.process_reply(
            "remember: lowercase works too",
            [],
            streamed=False,
        )
        mem = self._mem()
        self.assertEqual(len(mem), 1)
        self.assertIn("lowercase works", mem[0])

    def test_remember_alongside_other_directives(self):
        # REMEMBER fires BEFORE tool dispatch. Other directives still parse.
        # We monkeypatch run to record calls and verify both happen.
        run_calls = []
        orig_run = master_ai.confirm_run
        master_ai.confirm_run = lambda c: run_calls.append(c) or True
        try:
            master_ai.process_reply(
                "REMEMBER: pwd is a safe quick check\nRUN: pwd",
                [],
                streamed=False,
            )
        finally:
            master_ai.confirm_run = orig_run
        self.assertEqual(self._mem(), ["pwd is a safe quick check"])
        self.assertEqual(run_calls, ["pwd"])


class BlockedFeedbackInvitesRemember(unittest.TestCase):
    """The [TOOL BLOCKED] history message should mention REMEMBER so the
    model knows it can self-write a lesson on its next turn."""

    def test_tool_blocked_message_mentions_remember(self):
        import inspect

        src = inspect.getsource(master_ai.process_reply)
        self.assertIn("[TOOL BLOCKED]", src)
        # The blocked-feedback body must invite the model to emit REMEMBER:
        idx = src.find("[TOOL BLOCKED]")
        window = src[idx : idx + 1500]
        self.assertIn(
            "REMEMBER:",
            window,
            "TOOL BLOCKED feedback should invite a REMEMBER: line — "
            "that's how the model self-teaches from failures",
        )

    def test_hook_blocked_message_mentions_remember(self):
        import inspect

        src = inspect.getsource(master_ai.process_reply)
        idx = src.find("[HOOK BLOCKED]")
        self.assertGreater(idx, 0, "HOOK BLOCKED feedback must exist")
        window = src[idx : idx + 1000]
        self.assertIn(
            "REMEMBER:",
            window,
            "HOOK BLOCKED feedback should invite a REMEMBER: line too",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
