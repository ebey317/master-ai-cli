#!/usr/bin/env python3
"""Legacy-vs-typed parity tests for typed_actions.parse_reply_with_bodies()
(2026-08-23 gap-closing pass — see ~/.claude/plans/dynamic-wiggling-aurora.md).

Expected values in each case were hand-verified against master_ai.py's own
extraction logic (process_reply(), master_ai.py:9153-9370: _real_directive
backtick-parity at 9180-9184, the REMEMBER-in-body exclusion at 9235-9247,
the CREATE/EDIT <<<CONTENT/<<<FIND/<<<REPLACE body-block state machine at
9260-9298, and the fenced-code salvage fallback at 9300-9370) rather than by
importing master_ai.py directly. master_ai.py is the live 14k-line dispatch
path shared with confirm_run/confirm_create/etc.; the two live copies of it
(~/scripts and ~/projects/master-ai) have already diverged on unrelated
cloud-provider logic, so adding an import/helper dependency here would mean
keeping that wiring in sync in two safety-critical files for a test suite
that only needs to check typed_actions' own parsing. If master_ai.py's
extraction logic changes in a way that would change what these fixtures
should parse to, this suite will NOT catch that automatically — re-verify
the expected values by hand against master_ai.py's current extraction
before trusting a green run as proof of parity after such a change.

No master_ai import, no Ollama, no shell.

Run: python3 ~/master-ai-cli/test_typed_actions_parity.py
Exit: 0 = all green, non-zero = at least one parity mismatch.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.expanduser("~/scripts"))

import typed_actions as ta  # noqa: E402


class SingleLineDirectives(unittest.TestCase):
    def test_single_run(self):
        actions = ta.parse_reply_with_bodies("RUN: ls -la /tmp")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "RUN")
        self.assertEqual(actions[0].target, "ls -la /tmp")
        self.assertTrue(actions[0].requires_confirm)

    def test_single_read(self):
        actions = ta.parse_reply_with_bodies("READ: /home/elijah/notes.md")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "READ")
        self.assertEqual(actions[0].target, "/home/elijah/notes.md")
        self.assertFalse(actions[0].requires_confirm)

    def test_run_skill(self):
        actions = ta.parse_reply_with_bodies("RUN_SKILL: file_finder query=typed_actions")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "RUN_SKILL")
        self.assertEqual(actions[0].target, "file_finder query=typed_actions")
        self.assertTrue(actions[0].requires_confirm)

    def test_send_email(self):
        line = 'SEND_EMAIL: to=elijah@example.com subject="status" body="done"'
        actions = ta.parse_reply_with_bodies(line)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "SEND_EMAIL")
        self.assertIn("to=elijah@example.com", actions[0].target)
        self.assertTrue(actions[0].requires_confirm)

    def test_plan_done_think_are_safe_and_no_confirm(self):
        for kind, line in (
            ("PLAN", "PLAN: read the file first, then edit it"),
            ("DONE", "DONE: task finished"),
            ("THINK", "THINK: considering the two approaches"),
        ):
            with self.subTest(kind=kind):
                actions = ta.parse_reply_with_bodies(line)
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0].kind, kind)
                self.assertEqual(actions[0].risk, ta.Risk.SAFE)
                self.assertFalse(actions[0].requires_confirm)


class BacktickParitySuppression(unittest.TestCase):
    def test_directive_named_in_backticks_does_not_fire(self):
        # Mirrors the 2026-04-25 regression master_ai._real_directive fixed:
        # "files via `READ:`" must not fire READ on the rest of the sentence.
        text = "You can read files via `READ:` followed by a path."
        actions = ta.parse_reply_with_bodies(text)
        self.assertEqual(actions, [])

    def test_directive_outside_backticks_still_fires(self):
        # PLAN ONLY: RUN: cmd has zero backticks — must still fire RUN.
        text = "PLAN ONLY: RUN: echo hello"
        actions = ta.parse_reply_with_bodies(text)
        kinds = {a.kind for a in actions}
        self.assertIn("RUN", kinds)
        run_action = next(a for a in actions if a.kind == "RUN")
        self.assertEqual(run_action.target, "echo hello")


class CreateEditBodyBlocks(unittest.TestCase):
    def test_create_with_content_block(self):
        text = (
            "CREATE: /tmp/hello.py\n"
            "<<<CONTENT\n"
            "print('hello')\n"
            ">>>CONTENT\n"
        )
        actions = ta.parse_reply_with_bodies(text)
        creates = [a for a in actions if a.kind == "CREATE"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0].target, os.path.expanduser("/tmp/hello.py"))
        self.assertEqual(creates[0].create_content, "print('hello')")
        self.assertTrue(creates[0].requires_confirm)

    def test_edit_with_find_replace_blocks(self):
        text = (
            "EDIT: /tmp/hello.py\n"
            "<<<FIND\n"
            "print('hello')\n"
            ">>>FIND\n"
            "<<<REPLACE\n"
            "print('hello world')\n"
            ">>>REPLACE\n"
        )
        actions = ta.parse_reply_with_bodies(text)
        edits = [a for a in actions if a.kind == "EDIT"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].edit_old, "print('hello')")
        self.assertEqual(edits[0].edit_new, "print('hello world')")

    def test_create_via_fenced_code_salvage(self):
        # Bare CREATE: with no <<<CONTENT block, followed by a markdown
        # fence — master_ai.py:9300-9335 salvages this instead of doing
        # nothing.
        text = (
            "CREATE: /tmp/demo.py\n"
            "Here is the file:\n"
            "```python\n"
            "print('salvaged')\n"
            "```\n"
        )
        actions = ta.parse_reply_with_bodies(text)
        creates = [a for a in actions if a.kind == "CREATE"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0].target, os.path.expanduser("/tmp/demo.py"))
        self.assertEqual(creates[0].create_content, "print('salvaged')")

    def test_malformed_create_with_no_body_produces_no_content(self):
        # CREATE: with no proper body block and no fence to salvage from —
        # legacy triggers a [Directive repair] round-trip rather than
        # writing anything (master_ai.py:9372-9412); this module has no
        # repair-loop concept, so the correct outcome here is simply no
        # CREATE action at all rather than one with empty/garbage content.
        text = "CREATE: /tmp/nothing.py\nNo body, no fence, nothing to salvage.\n"
        actions = ta.parse_reply_with_bodies(text)
        creates = [a for a in actions if a.kind == "CREATE"]
        self.assertEqual(creates, [])

    def test_remember_inside_create_body_does_not_fire(self):
        text = (
            "CREATE: /tmp/note.py\n"
            "<<<CONTENT\n"
            "# REMEMBER: this is just a code comment, not a directive\n"
            "print('ok')\n"
            ">>>CONTENT\n"
        )
        actions = ta.parse_reply_with_bodies(text)
        remembers = [a for a in actions if a.kind == "REMEMBER"]
        self.assertEqual(remembers, [])
        creates = [a for a in actions if a.kind == "CREATE"]
        self.assertEqual(len(creates), 1)
        self.assertIn("REMEMBER: this is just a code comment", creates[0].create_content)


class MixedReply(unittest.TestCase):
    def test_reply_with_multiple_directive_kinds(self):
        text = (
            "READ: /tmp/input.txt\n"
            "RUN: cat /tmp/input.txt\n"
            "REMEMBER: the input file lives at /tmp/input.txt\n"
        )
        actions = ta.parse_reply_with_bodies(text)
        kinds = sorted(a.kind for a in actions)
        self.assertEqual(kinds, ["READ", "REMEMBER", "RUN"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
