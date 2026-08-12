#!/usr/bin/env python3
"""Focused tests for Chrome extension Phases 7-10 support code."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

# Repo dir must win over ~/scripts: on dev machines with an unrelated live
# deployment at ~/scripts (different subagent_registry.py, different
# subagents), that shadow would silently swap in the wrong module. Evict
# any stale cached copy too — module caching means an earlier test file's
# unguarded `import subagent_registry` can already have poisoned
# sys.modules before this file's own sys.path fix takes effect. Mirrors
# the guard in test_subagent_registry.py.
REPO_ROOT = Path(__file__).parent
# Always insert at position 0 (not just "if missing") — pytest's own
# rootdir insertion may already have REPO_ROOT further back in sys.path,
# which would make an "if not in sys.path" guard a no-op and leave
# ~/scripts (inserted above) winning the lookup.
sys.path.insert(0, str(REPO_ROOT))
if "subagent_registry" in sys.modules:
    del sys.modules["subagent_registry"]

import subagent_registry as sr  # noqa: E402
import typed_actions as ta  # noqa: E402
import sensei_native_host as nh  # noqa: E402
import stt_server  # noqa: E402

# sr.discover()'s default SUBAGENTS_DIR is ~/scripts/subagents, which on a
# dev machine with an unrelated live deployment there (different subagents:
# profile_fetcher/posting_inspector/application_logger, no find/
# workflow_describer) resolves before ever falling back to this repo's own
# subagents/ dir. Force discovery from the repo dir explicitly — same
# pattern test_subagent_registry.py already uses for the same reason.
sr.discover(REPO_ROOT / "subagents")


AX_TREE = {
    "buttons": [
        {"ref": "r-1", "role": "button", "name": "Submit application", "selector": "#submit"},
        {"ref": "r-2", "role": "button", "name": "Send my resume in", "selector": "#send"},
        {"ref": "r-3", "role": "button", "name": "Apply now", "selector": "#apply"},
        {"ref": "r-4", "role": "button", "name": "Cancel", "selector": "#cancel"},
    ],
    "inputs": [
        {"ref": "r-5", "role": "textbox", "name": "Email address", "selector": "#email"},
    ],
}


def _ensure_repo_subagents_registered():
    """stt_server._tool_find / _tool_describe_step do `import subagent_registry
    as _sr` fresh at call time, which resolves whatever object currently sits
    at sys.modules["subagent_registry"] — not necessarily the `sr` bound at
    this file's collection time. Another test file's own module-level
    sys.path/sys.modules juggling (e.g. test_subagent_registry.py's identical
    del+reimport guard) can swap in a freshly re-imported module object whose
    default auto-discover() only found ~/scripts/subagents (an unrelated
    deployment's subagents, if that directory happens to exist). Re-running
    discover() against whatever is current right before the call is what
    actually makes this deterministic, not sys.path ordering."""
    _sr = sys.modules.get("subagent_registry", sr)
    _sr.discover(REPO_ROOT / "subagents")
    return _sr


class SemanticFindTests(unittest.TestCase):
    def setUp(self):
        _ensure_repo_subagents_registered()

    def test_find_subagent_matches_paraphrased_apply_controls(self):
        result = sr.run("find", "apply button", context={"ax_tree": AX_TREE})
        names = [m["name"] for m in result["matches"][:3]]
        self.assertIn("Apply now", names)
        self.assertTrue(any(name in names for name in ("Submit application", "Send my resume in")))

    def test_tool_find_endpoint_helper_normalizes_shape(self):
        result = stt_server._tool_find({"query": "send resume", "ax_tree": AX_TREE})
        self.assertTrue(result["ok"])
        self.assertLessEqual(len(result["matches"]), 20)
        self.assertTrue(any(m["ref"] == "r-2" for m in result["matches"]))


class WorkflowDescribeTests(unittest.TestCase):
    def setUp(self):
        _ensure_repo_subagents_registered()

    def test_workflow_describer(self):
        result = stt_server._tool_describe_step({
            "step": {"kind": "BROWSER_FILL", "target": "#email", "value": "elijah@example.com"}
        })
        self.assertTrue(result["ok"])
        self.assertIn("Fill", result["description"])


class RemoteMcpTypedActionTests(unittest.TestCase):
    def test_remote_mcp_parses_as_typed_action(self):
        action = ta.parse_directive('REMOTE_MCP: {"server":"demo","method":"tools/list","params":{}}')
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "REMOTE_MCP")
        self.assertTrue(action.requires_confirm)
        self.assertEqual(action.risk, ta.Risk.SAFE)


class NativeHostTests(unittest.TestCase):
    def test_ping_pong(self):
        self.assertEqual(nh.handle_message({"type": "ping", "id": "1"}), {
            "type": "pong", "id": "1", "ok": True
        })

    def test_tool_request_refuses_missing_token(self):
        with tempfile.TemporaryDirectory() as td:
            old = nh.TOKEN_PATH
            try:
                nh.TOKEN_PATH = str(Path(td) / "token")
                Path(nh.TOKEN_PATH).write_text("secret", encoding="utf-8")
                result = nh.handle_message({
                    "type": "tool_request",
                    "id": "2",
                    "payload": {"endpoint": "/health"},
                })
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "auth_failed")
            finally:
                nh.TOKEN_PATH = old

    def test_tool_request_refuses_eval_payload(self):
        with tempfile.TemporaryDirectory() as td:
            old = nh.TOKEN_PATH
            try:
                nh.TOKEN_PATH = str(Path(td) / "token")
                Path(nh.TOKEN_PATH).write_text("secret", encoding="utf-8")
                result = nh.handle_message({
                    "type": "tool_request",
                    "id": "3",
                    "token": "secret",
                    "payload": {"endpoint": "/tool/find", "eval": "1+1"},
                })
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_code"], "eval_refused")
            finally:
                nh.TOKEN_PATH = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
