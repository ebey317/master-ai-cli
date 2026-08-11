#!/usr/bin/env python3
"""Unit tests for ~/scripts/subagent_registry.py and the 6 built-in
subagents shipped under ~/scripts/subagents/.

Covers:
- discover() finds the example subagents
- list_subagents() returns ≥ 6 entries
- get(name) and run(name, task) round-trip
- code_reviewer returns structured issues (no RUN: in output)
- directive_simulator parses without executing
- unknown subagent yields {'error': ...}
- broken subagent file doesn't crash the registry
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["SENSEI_TUI"] = "0"
REPO_ROOT = Path(__file__).parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if "subagent_registry" in sys.modules:
    del sys.modules["subagent_registry"]
import subagent_registry as sr  # noqa: E402


class DiscoveryAndListing(unittest.TestCase):
    def test_six_or_more_builtins_registered(self):
        sr.discover(Path(__file__).parent / "subagents")  # idempotent
        names = {a.name for a in sr.list_subagents()}
        for required in ("code_reviewer", "test_runner", "file_finder",
                         "directive_simulator", "context_inspector",
                         "spend_reporter"):
            self.assertIn(required, names,
                f"built-in subagent {required!r} not registered")
        self.assertGreaterEqual(len(names), 6,
            f"expected ≥6 subagents, got {len(names)}: {names}")

    def test_get_returns_known_and_none(self):
        sr.discover(Path(__file__).parent / "subagents")
        self.assertIsNotNone(sr.get("code_reviewer"))
        self.assertIsNone(sr.get("definitely_not_a_subagent_xyz"))
        self.assertIsNone(sr.get(None))


class RunWrapper(unittest.TestCase):
    def test_unknown_subagent_returns_error_dict(self):
        result = sr.run("definitely_not_a_subagent_xyz")
        self.assertIn("error", result)
        self.assertIn("available", result)

    def test_run_tags_result_with_subagent_name(self):
        # spend_reporter is safe to run — pure read on empty files yields zeros.
        result = sr.run("spend_reporter", "100")
        self.assertEqual(result.get("subagent"), "spend_reporter")
        self.assertIn("harvest", result)

    def test_subagent_exception_returned_as_error_dict(self):
        # Register a broken subagent that always raises.
        def boom(task, context=None):
            raise RuntimeError("intentional test failure")
        sr._REGISTRY["boom"] = sr.Subagent(
            name="boom", description="raises", run=boom,
            source="<test>", module_name="<test>",
        )
        try:
            result = sr.run("boom", "anything")
            self.assertIn("error", result)
            self.assertEqual(result["subagent"], "boom")
            self.assertEqual(result["exception"], "RuntimeError")
        finally:
            sr._REGISTRY.pop("boom", None)


class CodeReviewerBuiltin(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_structured_issues_no_directives(self):
        # Create a .py with a syntax error so we get a syntax issue.
        bad = Path(self.tmpdir) / "bad.py"
        bad.write_text("def foo(:\n    return 1\n")
        result = sr.run("code_reviewer", str(bad))
        self.assertIn("issues", result)
        self.assertGreater(len(result["issues"]), 0)
        # Each issue is structured data — has path/kind/msg keys.
        for i in result["issues"]:
            self.assertIn("path", i)
            self.assertIn("kind", i)
            self.assertIn("msg", i)
        # CRITICAL: result must NOT contain executable directives. The
        # executor would parse RUN: at line start; check across the
        # serialized JSON form.
        as_json = json.dumps(result)
        for verb in ("RUN:", "RUNTERM:", "READ:", "CREATE:", "EDIT:"):
            self.assertNotIn(f"\n{verb}", as_json,
                f"code_reviewer output contains {verb} — must stay inert")

    def test_handles_missing_file(self):
        result = sr.run("code_reviewer", "/tmp/definitely-not-here-xxxxx.py")
        self.assertIn("issues", result)
        self.assertEqual(result["issues"][0]["kind"], "missing")

    def test_empty_task_returns_error(self):
        result = sr.run("code_reviewer", "")
        self.assertIn("error", result)


class DirectiveSimulatorBuiltin(unittest.TestCase):
    def test_parses_directives_without_executing(self):
        reply = "I'll do this:\nRUN: ls /tmp\nCREATE: /tmp/new.py\nRUN: rm -rf /tmp/foo"
        result = sr.run("directive_simulator", reply)
        self.assertEqual(result["count"], 3)
        kinds = [a["kind"] for a in result["actions"]]
        self.assertEqual(kinds, ["RUN", "CREATE", "RUN"])
        # The rm -rf /tmp/foo must be flagged as high risk.
        self.assertGreater(len(result["high_risk"]), 0)

    def test_innocuous_reply_zero_actions(self):
        result = sr.run("directive_simulator", "Just chatting, no directives.")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["actions"], [])


class BrokenSubagentDoesNotCrashRegistry(unittest.TestCase):
    """Drop a .py with a syntax error into a temp subagents dir, verify
    discover() in that dir doesn't crash and still loads valid siblings.
    """

    def test_broken_sibling_skipped(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            # One broken
            (tmp / "broken.py").write_text("def foo(:\n  pass\n")
            # One valid
            (tmp / "valid_subagent.py").write_text(
                'name = "valid_x"\n'
                'description = "ok"\n'
                'def run(task, context=None):\n'
                '    return {"ok": True}\n'
            )
            # Snapshot real registry, point discover at the temp dir.
            saved = dict(sr._REGISTRY)
            sr.clear()
            sr.discover(tmp)
            self.assertIn("valid_x", {a.name for a in sr.list_subagents()})
            # Re-discover the real builtins so subsequent tests pass.
            sr.clear()
            sr._REGISTRY.update(saved)
            sr.discover(Path(__file__).parent / "subagents")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
