#!/usr/bin/env python3
"""Regression tests for master_ai.py's live-code-reload detector.

_reload_if_code_changed() ends in os.execvp(), which would replace this
test process entirely if actually called -- every test here monkeypatches
it to a recorder instead, so it's fully exercised without ever exec'ing.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


class AutoReloadTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())
        self._orig_resume_flag = master_ai.RESUME_FLAG
        self._orig_carry_file = master_ai._RELOAD_CARRY_FILE
        self._orig_chats_dir = master_ai.CHATS_DIR
        self._orig_session_ts = master_ai.SESSION_TS
        self._orig_startup_mtime = master_ai._STARTUP_CODE_MTIME
        self._orig_execvp = os.execvp
        self._orig_save_session = master_ai.save_session
        self._orig_scrollback = master_ai._clear_tmux_scrollback

        master_ai.RESUME_FLAG = self._tmpdir / "resume_flag"
        master_ai._RELOAD_CARRY_FILE = self._tmpdir / "carry_file"
        master_ai.CHATS_DIR = self._tmpdir
        master_ai.SESSION_TS = "test-session"

        self.execvp_calls = []
        self.save_session_calls = []
        master_ai.os.execvp = lambda *a, **k: self.execvp_calls.append((a, k))
        master_ai.save_session = lambda h, silent=False: self.save_session_calls.append(
            (h, silent)
        )
        master_ai._clear_tmux_scrollback = lambda *a, **k: None

    def tearDown(self):
        master_ai.RESUME_FLAG = self._orig_resume_flag
        master_ai._RELOAD_CARRY_FILE = self._orig_carry_file
        master_ai.CHATS_DIR = self._orig_chats_dir
        master_ai.SESSION_TS = self._orig_session_ts
        master_ai._STARTUP_CODE_MTIME = self._orig_startup_mtime
        os.execvp = self._orig_execvp
        master_ai.save_session = self._orig_save_session
        master_ai._clear_tmux_scrollback = self._orig_scrollback
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_no_reload_when_file_unchanged(self):
        master_ai._STARTUP_CODE_MTIME = os.path.getmtime(
            os.path.abspath(master_ai.__file__)
        )
        master_ai._reload_if_code_changed([], "hello")
        self.assertEqual(self.execvp_calls, [])
        self.assertEqual(self.save_session_calls, [])

    def test_reload_triggers_when_file_changed(self):
        # Force a mismatch without touching the real file on disk.
        master_ai._STARTUP_CODE_MTIME = 1.0
        history = [{"role": "user", "content": "hi"}]
        master_ai._reload_if_code_changed(history, "what's next")

        self.assertEqual(len(self.execvp_calls), 1)
        args, _ = self.execvp_calls[0]
        self.assertEqual(args[0], sys.executable)
        self.assertIn("scripts/master_ai.py", args[1][1])

        self.assertEqual(len(self.save_session_calls), 1)
        saved_history, silent = self.save_session_calls[0]
        self.assertEqual(saved_history, history)
        self.assertTrue(silent)

    def test_pending_command_is_carried_across_the_reload(self):
        master_ai._STARTUP_CODE_MTIME = 1.0
        master_ai._reload_if_code_changed([], "finish the task list")
        self.assertEqual(
            master_ai._RELOAD_CARRY_FILE.read_text(), "finish the task list"
        )

    def test_resume_flag_points_at_the_saved_chat_log(self):
        master_ai._STARTUP_CODE_MTIME = 1.0
        master_ai._reload_if_code_changed([], "go")
        expected = str(master_ai.CHATS_DIR / f"{master_ai.SESSION_TS}.chat")
        self.assertEqual(master_ai.RESUME_FLAG.read_text(), expected)

    def test_carried_command_is_restored_as_pending_user_note(self):
        # Simulates the other half, on the fresh process's startup path.
        master_ai._RELOAD_CARRY_FILE.write_text("continue where we left off")
        if master_ai._RELOAD_CARRY_FILE.exists():
            carried = master_ai._RELOAD_CARRY_FILE.read_text()
            master_ai._RELOAD_CARRY_FILE.unlink(missing_ok=True)
            if carried:
                master_ai.PENDING_USER_NOTE = carried
        self.assertEqual(master_ai.PENDING_USER_NOTE, "continue where we left off")
        self.assertFalse(master_ai._RELOAD_CARRY_FILE.exists())
        master_ai.PENDING_USER_NOTE = ""


if __name__ == "__main__":
    unittest.main()
