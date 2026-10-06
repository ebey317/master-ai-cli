#!/usr/bin/env python3
"""Regression tests for structured session-resume state (runtime_state).

Covers the 2026-10-05 gap: save_session() wrote only .chat/.summary —
pending approvals, subagent dispatch feedback, and tool-lifecycle state
were lost on every resume. These tests pin the round-trip (collect →
serialize → write → load → restore) and the fail-open contract:

  * approvals restore as PENDING, never auto-approved, never dropped;
  * subagent records re-enter history as inert [SUBAGENT RESULT] context;
  * lifecycle globals come back so the next process_reply() turn surfaces
    [TOOL BLOCKED] / [User declined ...] / [HOOK BLOCKED] as before;
  * any drift (corrupt JSON, schema mismatch, wrong shape, stale file)
    logs loudly and falls back — resume never crashes.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import approval_queue  # noqa: E402
import runtime_state  # noqa: E402


def _entry(**over):
    """A realistic PENDING entry the way master_ai._queue_for_approval writes."""
    e = {
        "id": "20261005120000",
        "ts": time.time(),
        "status": "PENDING",
        "type": "run_command",
        "who": "master_ai.confirm_run",
        "what": "apt install foo",
        "where": "/home/elijah",
        "why": "no live terminal to confirm",
        "when": "next Elijah review",
        "how": "run_command(cmd) on approval",
        "diff": "+ Install foo",
        "trigger": "install foo for me",
        "payload": {"cmd": "apt install foo"},
    }
    e.update(over)
    return e


class TestCollect(unittest.TestCase):
    def test_collect_state_shape(self):
        st = runtime_state.collect_state(
            [{"role": "user", "content": "[SUBAGENT RESULT] goal='x' ok=True\nhello"}]
        )
        self.assertEqual(st["schema"], runtime_state.STATE_SCHEMA)
        self.assertIn("pending_approvals", st)
        self.assertIn("subagents", st)
        self.assertIn("tool_lifecycle", st)
        tls = st["tool_lifecycle"]
        for key in (
            "last_blocked_action",
            "last_denied_action",
            "last_hook_block",
            "recent_typed_actions",
            "pending_continuation",
            "pending_plan",
            "mode",
        ):
            self.assertIn(key, tls)

    def test_collect_subagents_picks_recent_blocks(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "[SUBAGENT RESULT] goal='a'\nbody a"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "[SUBAGENT ERROR] boom"},
        ]
        recent = runtime_state.collect_subagents(history)
        self.assertEqual(len(recent), 2)
        self.assertTrue(recent[0][1].startswith("[SUBAGENT RESULT]"))
        self.assertTrue(recent[1][1].startswith("[SUBAGENT ERROR]"))

    def test_collect_tool_lifecycle_round_trips_continuation_and_plan(self):
        tls = runtime_state.collect_tool_lifecycle(
            blocked={"kind": "run", "command": "rm -rf /", "reason": "x"},
            denied={"kind": "run", "command": "ls"},
            hook_block={"kind": "edit", "path": "/a", "hook_id": "h"},
            live_typed=[{"kind": "RUN", "target": "ls", "status": "completed"}],
            pending_continuation={
                "provider": "groq",
                "messages": [{"role": "user", "content": "hi"}],
                "so_far": "partial answer",
            },
            pending_plan_text="1. do the thing",
            pending_plan_request="do the thing",
            mode="auto",
        )
        tc = tls["pending_continuation"]
        self.assertEqual(tc["provider"], "groq")
        self.assertEqual(tc["so_far"], "partial answer")
        self.assertTrue(tls["pending_plan"]["text"].startswith("1. do"))
        self.assertEqual(tls["mode"], "auto")
        self.assertEqual(tls["last_blocked_action"]["kind"], "run")

    def test_collect_tool_lifecycle_strips_file_bodies(self):
        tls = runtime_state.collect_tool_lifecycle(
            live_typed=[
                {
                    "kind": "CREATE",
                    "target": "/tmp/x.py",
                    "create_content": "print('huge body')",
                    "edit_new": "stuff",
                }
            ],
        )
        self.assertEqual(tls["recent_typed_actions"][0].get("create_content"), None)
        self.assertEqual(tls["recent_typed_actions"][0].get("edit_new"), None)


class TestWriteLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._chat = self._tmp / "123.chat"

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_write_then_load_round_trips(self):
        st = runtime_state.collect_state([])
        st["tool_lifecycle"]["pending_plan"] = {
            "request": "r",
            "text": "plan text",
        }
        runtime_state.write_state(
            st, runtime_state.state_path_for(self._chat), _atomic_noop_free
        )
        loaded = runtime_state.load_state(runtime_state.state_path_for(self._chat))
        self.assertEqual(loaded["tool_lifecycle"]["pending_plan"]["text"], "plan text")

    def test_load_missing_file_raises_schema_error(self):
        with self.assertRaises(runtime_state.StateSchemaError):
            runtime_state.load_state(self._tmp / "absent.chat")

    def test_load_corrupt_json_raises_schema_error(self):
        self._chat.write_text("{not json at all")
        with self.assertRaises(runtime_state.StateSchemaError):
            runtime_state.load_state(self._chat)

    def test_load_wrong_schema_version_raises_schema_error(self):
        self._chat.write_text(
            json.dumps({"schema": "session_state_v999", "saved_at": 0})
        )
        with self.assertRaises(runtime_state.StateSchemaError):
            runtime_state.load_state(self._chat)

    def test_load_non_dict_raises_schema_error(self):
        self._chat.write_text("[1, 2, 3]")
        with self.assertRaises(runtime_state.StateSchemaError):
            runtime_state.load_state(self._chat)

    def test_load_stale_snapshot_raises_schema_error(self):
        self._chat.write_text(
            json.dumps({"schema": runtime_state.STATE_SCHEMA, "saved_at": 0})
        )
        old = time.time() - (8 * 24 * 3600)
        os.utime(self._chat, (old, old))
        with self.assertRaises(runtime_state.StateSchemaError):
            runtime_state.load_state(self._chat)

    def test_load_returns_entries_for_valid_state(self):
        self._chat.write_text(
            json.dumps(
                {
                    "schema": runtime_state.STATE_SCHEMA,
                    "saved_at": time.time(),
                    "pending_approvals": [_entry()],
                    "subagents": {"recent": [[time.time(), "[SUBAGENT RESULT] x"]]},
                    "tool_lifecycle": runtime_state.collect_tool_lifecycle(),
                }
            )
        )
        st = runtime_state.load_state(self._chat)
        self.assertEqual(len(st["pending_approvals"]), 1)


def _atomic_noop_free(path, text):  # matches _atomic_write_text's signature
    Path(path).write_text(text)


class TestRestore(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._orig_jsonl = approval_queue.JSONL_FILE
        approval_queue.JSONL_FILE = self._tmp / ".pending_actions.jsonl"

    def tearDown(self):
        approval_queue.JSONL_FILE = self._orig_jsonl
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_pending_approvals_come_back_pending(self):
        n = runtime_state.restore_pending_approvals([_entry()])
        self.assertEqual(n, 1)
        pending = approval_queue.list_pending()
        self.assertEqual(len(pending), 1)
        e = pending[0]
        self.assertEqual(e["status"], "PENDING")
        self.assertEqual(e["payload"], {"cmd": "apt install foo"})
        self.assertIn("resumed", e["why"])

    def test_restored_approvals_get_fresh_ids(self):
        runtime_state.restore_pending_approvals(
            [_entry(), _entry(id="20261005120000-01")]
        )
        ids = [e["id"] for e in approval_queue.list_pending()]
        self.assertEqual(len(ids), 2)
        self.assertTrue(all("2026" in i for i in ids))

    def test_restore_approvals_tolerates_bad_entries(self):
        n = runtime_state.restore_pending_approvals(
            [None, "junk", _entry(payload=None), _entry()]
        )
        self.assertEqual(n, 2)

    def test_restore_subagents_injects_context_turns(self):
        history: list = []
        n = runtime_state.restore_subagents(
            [[time.time(), "[SUBAGENT RESULT] goal='x' ok=True\nbody"]], history
        )
        self.assertEqual(n, 1)
        self.assertEqual(history[0]["role"], "user")
        self.assertIn("[SUBAGENT RESULT]", history[0]["content"])
        self.assertEqual(history[-1]["role"], "assistant")

    def test_restore_subagents_rejects_garbage(self):
        history: list = []
        # Malformed items are skipped individually — a drifted entry must
        # degrade the restore, never crash it.
        self.assertEqual(
            runtime_state.restore_subagents([[1, "  "], ["x"], "no", None], history),
            0,
        )
        self.assertEqual(history, [])

    def test_restore_subagents_mixed_good_and_bad(self):
        history: list = []
        n = runtime_state.restore_subagents(
            [[1, "  "], [time.time(), "[SUBAGENT RESULT] ok\nbody"], None], history
        )
        self.assertEqual(n, 1)

    def test_restore_tool_lifecycle_applies_via_callback(self):
        held: dict = {}

        def _apply(name, value):
            held[name] = value

        s = runtime_state.restore_tool_lifecycle(
            {
                "last_blocked_action": {"kind": "run", "command": "x"},
                "last_denied_action": {"kind": "create", "path": "/p"},
                "last_hook_block": {"hook_id": "h", "reason": "r"},
                "recent_typed_actions": [{"kind": "RUN", "target": "ls"}],
                "pending_continuation": {
                    "provider": "groq",
                    "messages": [],
                    "so_far": "partial",
                },
                "pending_plan": {"request": "r", "text": "plan"},
            },
            _apply,
        )
        self.assertTrue(s["blocked"])
        self.assertTrue(s["denied"])
        self.assertTrue(s["hook_block"])
        self.assertTrue(s["continuation"])
        self.assertTrue(s["plan"])
        self.assertEqual(s["typed_actions"], 1)
        self.assertEqual(held["_LAST_BLOCKED_ACTION"]["command"], "x")
        self.assertEqual(held["PENDING_CONTINUATION"]["so_far"], "partial")
        self.assertEqual(held["PENDING_PLAN_TEXT"], "plan")

    def test_restore_tool_lifecycle_ignores_bad_mode_and_empty(self):
        held: dict = {}

        def _apply(name, value):
            held[name] = value

        s = runtime_state.restore_tool_lifecycle(
            {"mode": "not-a-mode", "last_blocked_action": {}}, _apply
        )
        self.assertEqual(s["mode"], "")
        self.assertFalse(s["blocked"])
        self.assertNotIn("_LAST_BLOCKED_ACTION", held)

    def test_restore_summary_line_mentions_all_kinds(self):
        line = runtime_state.restore_summary_line(
            {
                "approvals_restored": 2,
                "subagents_restored": 1,
                "lifecycle": {"continuation": True, "plan": True},
            }
        )
        self.assertIn("2 pending approval", line)
        self.assertIn("subagent", line)
        self.assertIn("'proceed'", line)
        self.assertIn("plan", line)


class TestMasterAiIntegration(unittest.TestCase):
    """End-to-end through master_ai.save_session / _restore_structured_state."""

    def setUp(self):
        import master_ai

        self._ma = master_ai
        self._tmp = Path(tempfile.mkdtemp())
        self._orig = {
            "CHATS_DIR": master_ai.CHATS_DIR,
            "SESSION_TS": master_ai.SESSION_TS,
            "JSONL": approval_queue.JSONL_FILE,
        }
        master_ai.CHATS_DIR = self._tmp
        master_ai.SESSION_TS = "itest"
        approval_queue.JSONL_FILE = self._tmp / ".pending_actions.jsonl"

    def tearDown(self):
        for k, v in self._orig.items():
            if k == "JSONL":
                approval_queue.JSONL_FILE = v
            else:
                setattr(self._ma, k, v)
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_save_session_writes_state_file_alongside_chat(self):
        history = [
            {"role": "user", "content": "run the audit"},
            {
                "role": "assistant",
                "content": "queued",
            },
            {"role": "user", "content": "[SUBAGENT RESULT] goal='a'\nbody a"},
        ]
        self._ma.save_session(history, silent=True, summarize_timeout=0.01)
        state_path = self._tmp / "itest.state.json"
        self.assertTrue(state_path.exists(), "state file must be written")
        st = json.loads(state_path.read_text())
        self.assertEqual(st["schema"], runtime_state.STATE_SCHEMA)
        self.assertEqual(len(st["subagents"]["recent"]), 1)

    def test_save_session_survives_state_write_failure(self):
        # Force _atomic_write_text to blow up only for the state file by
        # pointing state_path_for at a directory (os.replace fails there).
        orig_spf = runtime_state.state_path_for
        runtime_state.state_path_for = lambda p: self._tmp  # a directory
        try:
            history = [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ]
            self._ma.save_session(history, silent=True, summarize_timeout=0.01)
            self.assertTrue(
                (self._tmp / "itest.chat").exists(),
                ".chat must still be written when the state write fails",
            )
        finally:
            runtime_state.state_path_for = orig_spf

    def test_restore_from_saved_session_end_to_end(self):
        history = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        self._ma._LAST_BLOCKED_ACTION = {
            "kind": "run",
            "command": "rm -rf /tmp/x",
            "reason": "destructive",
        }
        try:
            self._ma.save_session(history, silent=True, summarize_timeout=0.01)
        finally:
            self._ma._LAST_BLOCKED_ACTION = {}
        chat_path = self._tmp / "itest.chat"
        loaded: list = []
        summary = self._ma._restore_structured_state(chat_path, loaded)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["lifecycle"]["blocked"], True)
        self.assertEqual(loaded, [])  # no approvals/subagents in snapshot
        # Restore lifecycle only through the public helper to verify plumbing:
        st = runtime_state.load_state(runtime_state.state_path_for(chat_path))
        held: dict = {}

        def _apply(name, value):
            held[name] = value

        s = runtime_state.restore_tool_lifecycle(st["tool_lifecycle"], _apply)
        self.assertEqual(held["_LAST_BLOCKED_ACTION"]["reason"], "destructive")
        self.assertTrue(s["blocked"])

    def test_restore_drifted_state_returns_none(self):
        history = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "ok"},
        ]
        self._ma.save_session(history, silent=True, summarize_timeout=0.01)
        chat_path = self._tmp / "itest.chat"
        state_path = chat_path.with_suffix(".state.json")
        state_path.write_text('{"schema": "wrong_v9000", "saved_at": 1}')
        self.assertIsNone(self._ma._restore_structured_state(chat_path, []))
        # Corrupt JSON — same fail-open contract.
        state_path.write_text("garbage{")
        self.assertIsNone(self._ma._restore_structured_state(chat_path, []))
        # Missing file — same contract.
        state_path.unlink()
        self.assertIsNone(self._ma._restore_structured_state(chat_path, []))

    def test_resume_restores_pending_approval_end_to_end(self):
        # Queue one entry through the real queue file, save, restore.
        eid = approval_queue.queue(
            entry_type="run_command",
            who="test",
            what="echo hi",
            where="/tmp",
            why="test queue",
            payload={"cmd": "echo hi"},
        )
        self.assertTrue(eid)
        history = [
            {"role": "user", "content": "run echo hi"},
            {"role": "assistant", "content": "needs approval"},
        ]
        self._ma.save_session(history, silent=True, summarize_timeout=0.01)
        # Simulate the resume: fresh (empty) queue file, same state snapshot.
        approval_queue.JSONL_FILE = self._tmp / ".pending_actions_restored.jsonl"
        self._ma._restore_structured_state(self._tmp / "itest.chat", [])
        pending = approval_queue.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["payload"], {"cmd": "echo hi"})
        self.assertEqual(pending[0]["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
