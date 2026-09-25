#!/usr/bin/env python3
"""Unit tests for ~/scripts/hooks.py (P1.4).

Verifies the registry + built-in hooks (syntax-check on post_edit /
post_create, secret-scan on pre_create) + JSON loader + enable/disable.
No master_ai import — keeps this fast and isolated.

Run: python3 ~/scripts/test_hooks.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/scripts"))

import hooks  # noqa: E402


class RegistryShape(unittest.TestCase):
    def test_kinds_are_complete(self):
        # on_blocked added 2026-05-11 for auto-extract-lesson hook.
        # turn_answer_start added 2026-05-15 (Phase 5.6) — observer-only,
        # fires once per /chat response before the reply is returned.
        expected = {
            "pre_run",
            "post_run",
            "pre_runterm",
            "post_runterm",
            "pre_read",
            "post_read",
            "pre_create",
            "post_create",
            "pre_edit",
            "post_edit",
            "on_blocked",
            "turn_answer_start",
        }
        self.assertEqual(hooks.KINDS, frozenset(expected))

    def test_builtin_hooks_present(self):
        ids = {h.id for h in hooks.list_hooks() if h.source == "builtin"}
        self.assertIn("syntax-check-py-post-edit", ids)
        self.assertIn("syntax-check-py-post-create", ids)
        self.assertIn("syntax-check-sh-post-edit", ids)
        self.assertIn("syntax-check-sh-post-create", ids)
        self.assertIn("secret-scan-pre-create", ids)

    def test_builtins_enabled_by_default(self):
        for h in hooks.list_hooks():
            if h.source == "builtin":
                self.assertTrue(h.enabled, f"{h.id} should be enabled by default")

    def test_register_rejects_unknown_kind(self):
        reg = hooks.HookRegistry()
        with self.assertRaises(ValueError):
            reg.register(
                hooks.Hook(
                    id="bad", kind="pre_dance", fn=lambda *a, **kw: hooks.FireResult()
                )
            )

    def test_enable_disable(self):
        # Pull a built-in out and back in.
        self.assertTrue(hooks.disable("syntax-check-py-post-edit"))
        h = next(h for h in hooks.list_hooks() if h.id == "syntax-check-py-post-edit")
        self.assertFalse(h.enabled)
        self.assertTrue(hooks.enable("syntax-check-py-post-edit"))
        self.assertTrue(h.enabled)

    def test_enable_disable_unknown_returns_false(self):
        self.assertFalse(hooks.enable("nonexistent-hook-id-xyz"))
        self.assertFalse(hooks.disable("nonexistent-hook-id-xyz"))


class SyntaxCheckHook(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_valid_python_passes(self):
        p = Path(self.tmpdir) / "good.py"
        p.write_text("x = 1\ny = 2\nprint(x + y)\n")
        r = hooks.fire("post_edit", str(p))
        self.assertFalse(r.blocked, f"valid .py should not block: {r.reason}")

    def test_broken_python_blocks(self):
        p = Path(self.tmpdir) / "bad.py"
        # Deliberate SyntaxError
        p.write_text("def foo(:\n    return 1\n")
        r = hooks.fire("post_edit", str(p))
        self.assertTrue(r.blocked, "deliberate SyntaxError should block on post_edit")
        self.assertIn("syntax", r.reason.lower())
        self.assertIn("syntax-check-py", r.hook_id)

    def test_broken_python_blocks_on_post_create_too(self):
        p = Path(self.tmpdir) / "newfile.py"
        p.write_text("def foo(:\n    return 1\n")
        r = hooks.fire("post_create", str(p))
        self.assertTrue(r.blocked)
        self.assertIn("syntax-check-py", r.hook_id)

    def test_non_py_path_passes_through(self):
        p = Path(self.tmpdir) / "readme.md"
        p.write_text("# title\nbody\n")
        r = hooks.fire("post_edit", str(p))
        self.assertFalse(r.blocked)

    def test_missing_file_passes_through(self):
        r = hooks.fire("post_edit", "/tmp/does-not-exist-xxxxx.py")
        self.assertFalse(r.blocked)


class ShellSyntaxCheckHook(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_valid_shell_passes(self):
        p = Path(self.tmpdir) / "good.sh"
        p.write_text("#!/bin/bash\necho hello\nfor i in 1 2 3; do echo $i; done\n")
        r = hooks.fire("post_edit", str(p))
        self.assertFalse(r.blocked)

    def test_broken_shell_blocks(self):
        p = Path(self.tmpdir) / "bad.sh"
        # Unmatched `do` without `done`
        p.write_text("#!/bin/bash\nfor i in 1 2 3; do\n  echo $i\n")
        r = hooks.fire("post_edit", str(p))
        self.assertTrue(r.blocked)
        self.assertIn("syntax-check-sh", r.hook_id)

    def test_broken_shell_blocks_on_post_create(self):
        p = Path(self.tmpdir) / "bad.sh"
        p.write_text("if true; then\n  echo yes\n")
        r = hooks.fire("post_create", str(p))
        self.assertTrue(r.blocked)

    def test_non_shell_path_passes(self):
        p = Path(self.tmpdir) / "notes.md"
        p.write_text("# title\n")
        r = hooks.fire("post_edit", str(p))
        self.assertFalse(r.blocked)


class SecretScanHook(unittest.TestCase):
    def test_aws_key_blocks(self):
        content = "config = {\n    'aws_key': 'AKIAIOSFODNN7EXAMPLE',\n}\n"
        r = hooks.fire("pre_create", content)
        self.assertTrue(r.blocked, "AWS access key id should trigger block")
        self.assertIn("secret-scan", r.hook_id)

    def test_private_key_blocks(self):
        content = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAA...\n-----END OPENSSH PRIVATE KEY-----\n"
        r = hooks.fire("pre_create", content)
        self.assertTrue(r.blocked)
        self.assertIn("secret-scan", r.hook_id)

    def test_github_token_blocks(self):
        content = "x = 'ghp_abcdefghij1234567890abcdefghij1234567890'  # github\n"
        r = hooks.fire("pre_create", content)
        self.assertTrue(r.blocked)

    def test_plain_short_string_passes_through(self):
        # Short string, no newlines — heuristic treats it as a path/name
        # and skips the scan (no real content to inspect).
        r = hooks.fire("pre_create", "/tmp/foo.txt")
        self.assertFalse(r.blocked)

    def test_innocuous_content_passes(self):
        content = "def add(a, b):\n    return a + b\n"
        r = hooks.fire("pre_create", content)
        self.assertFalse(r.blocked)


class FireUnknownKindReturnsResult(unittest.TestCase):
    def test_unknown_kind_does_not_raise(self):
        r = hooks.fire("pre_dance", "/tmp/foo")
        self.assertFalse(r.blocked)
        self.assertEqual(r.hook_id, "<unknown-kind>")


class JsonLoader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        try:
            self.path.unlink()
        except Exception:
            pass

    def test_loads_user_hook_default_disabled(self):
        data = {
            "version": 1,
            "hooks": [
                {"id": "lint-after-edit", "kind": "post_edit", "shell": "echo {target}"}
            ],
        }
        self.path.write_text(json.dumps(data))
        n = hooks.reload_user_hooks(self.path)
        self.assertEqual(n, 1)
        h = next((h for h in hooks.list_hooks() if h.id == "lint-after-edit"), None)
        self.assertIsNotNone(h)
        self.assertEqual(h.kind, "post_edit")
        self.assertFalse(h.enabled, "user hooks must default to enabled=False")
        # Built-ins still here
        self.assertTrue(
            any(h.id == "syntax-check-py-post-edit" for h in hooks.list_hooks())
        )
        # Reload clears prior user hooks
        self.path.write_text(json.dumps({"version": 1, "hooks": []}))
        hooks.reload_user_hooks(self.path)
        self.assertFalse(any(h.id == "lint-after-edit" for h in hooks.list_hooks()))

    def test_malformed_entry_skipped(self):
        data = {
            "version": 1,
            "hooks": [
                {"id": "missing-kind", "shell": "echo x"},  # no kind
                {"id": "bad-kind", "kind": "pre_dance", "shell": "echo x"},
                {"id": "ok-one", "kind": "post_edit", "shell": "echo ok"},
                "not a dict",
            ],
        }
        self.path.write_text(json.dumps(data))
        n = hooks.reload_user_hooks(self.path)
        self.assertEqual(n, 1)

    def test_missing_file_returns_zero(self):
        nonexistent = Path("/tmp/definitely-not-here-xxxxx-hooks.json")
        n = hooks.reload_user_hooks(nonexistent)
        self.assertEqual(n, 0)


class DisabledBuiltinDoesNotBlock(unittest.TestCase):
    def test_disabled_syntax_hook_lets_broken_py_through(self):
        # Use a temp .py with a syntax error and confirm fire returns
        # unblocked when the hook is disabled.
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
        tmp.write("def foo(:\n    pass\n")
        tmp.close()
        try:
            hooks.disable("syntax-check-py-post-edit")
            r = hooks.fire("post_edit", tmp.name)
            self.assertFalse(r.blocked, "disabled hook should not block")
        finally:
            hooks.enable("syntax-check-py-post-edit")
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
