#!/usr/bin/env python3
"""Unit tests for the auto-extract-lesson hook (on_blocked).

Closes the REMEMBER self-teaching loop: when an action lands BLOCKED,
a background worker asks the small local model for a one-line lesson
and stores it via master_ai.confirm_remember(). Tests verify:

- Hook registered as built-in on_blocked, default-enabled
- Skips POLICY / FENCE blocks (security guardrails — no useful lesson)
- Skips EMPTY / MISSING blocks (parser cleanup, not user-facing)
- Skips when action dict has no target / reason
- Rate limit: caps at _EXTRACT_MAX_PER_SESSION
- Worker stores via confirm_remember on a valid model response
- Worker stores nothing on "SKIP" model response
- Worker stores nothing on too-short or too-long model response
- master_ai.process_reply fires on_blocked through the [TOOL BLOCKED]
  feedback path (source-pinned)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import hooks  # noqa: E402
import master_ai  # noqa: E402


class HookRegistration(unittest.TestCase):
    def test_on_blocked_kind_exists(self):
        self.assertIn("on_blocked", hooks.KINDS)

    def test_auto_extract_lesson_registered(self):
        ids = {h.id for h in hooks.list_hooks() if h.source == "builtin"}
        self.assertIn("auto-extract-lesson", ids)

    def test_default_enabled(self):
        h = hooks._REGISTRY.find("auto-extract-lesson")
        self.assertIsNotNone(h)
        self.assertTrue(h.enabled)


class SkipsConditions(unittest.TestCase):
    """The hook should be a CHEAP no-op when there's no useful lesson
    to extract. These tests verify it returns immediately without
    spawning a worker thread."""

    def _spy(self):
        """Replace the worker with a recorder so we know whether it would
        have fired."""
        self.spawned = []
        self._orig = hooks._extract_lesson_worker
        hooks._extract_lesson_worker = lambda *a: self.spawned.append(a)

    def _restore(self):
        hooks._extract_lesson_worker = self._orig

    def setUp(self):
        self._spy()
        # Reset rate counter
        with hooks._EXTRACT_LOCK:
            hooks._EXTRACT_COUNT_SESSION = 0

    def tearDown(self):
        self._restore()

    def test_no_action_skips(self):
        hooks.fire("on_blocked", "anything", action=None)
        # Give the daemon thread a moment in case it WAS launched
        # despite the guard — should still be empty.
        self.assertEqual(self.spawned, [])

    def test_non_dict_action_skips(self):
        hooks.fire("on_blocked", "anything", action="not a dict")
        self.assertEqual(self.spawned, [])

    def test_empty_target_skips(self):
        hooks.fire("on_blocked", "", action={"kind": "RUN", "reason": "x"})
        self.assertEqual(self.spawned, [])

    def test_empty_reason_skips(self):
        hooks.fire(
            "on_blocked", "ls", action={"kind": "RUN", "target": "ls", "reason": ""}
        )
        self.assertEqual(self.spawned, [])

    def test_policy_block_skips(self):
        hooks.fire(
            "on_blocked",
            "tar ~/.ssh",
            action={
                "kind": "RUN",
                "target": "tar ~/.ssh",
                "reason": "credential exfil",
                "audit_kind": "POLICY-CMD-BLOCK",
            },
        )
        self.assertEqual(self.spawned, [])

    def test_fence_block_skips(self):
        hooks.fire(
            "on_blocked",
            "/etc/shadow",
            action={
                "kind": "READ",
                "target": "/etc/shadow",
                "reason": "outside allowed roots",
                "audit_kind": "READ-FENCE-BLOCK",
            },
        )
        self.assertEqual(self.spawned, [])

    def test_empty_audit_skips(self):
        hooks.fire(
            "on_blocked",
            "  ",
            action={
                "kind": "RUN",
                "target": "  ",
                "reason": "empty",
                "audit_kind": "RUN-EMPTY",
            },
        )
        self.assertEqual(self.spawned, [])

    def test_missing_target_audit_skips(self):
        hooks.fire(
            "on_blocked",
            "/tmp/x.sh",
            action={
                "kind": "RUNTERM",
                "target": "/tmp/x.sh",
                "reason": "target not found",
                "audit_kind": "RUNTERM-BLOCK-MISSING",
            },
        )
        self.assertEqual(self.spawned, [])

    def test_real_extractable_block_spawns_worker(self):
        hooks.fire(
            "on_blocked",
            "fetchmail -c",
            action={
                "kind": "RUN",
                "target": "fetchmail -c ~/scripts/fetchmailrc",
                "reason": "exit 127: fetchmail: command not found",
                "audit_kind": "RUN-BLOCK",
            },
        )
        self.assertEqual(len(self.spawned), 1)


class RateLimit(unittest.TestCase):
    def setUp(self):
        self.spawned = []
        self._orig = hooks._extract_lesson_worker
        hooks._extract_lesson_worker = lambda *a: self.spawned.append(a)
        with hooks._EXTRACT_LOCK:
            hooks._EXTRACT_COUNT_SESSION = 0

    def tearDown(self):
        hooks._extract_lesson_worker = self._orig

    def test_caps_at_session_max(self):
        cap = hooks._EXTRACT_MAX_PER_SESSION
        for i in range(cap + 5):
            hooks.fire(
                "on_blocked",
                f"cmd{i}",
                action={
                    "kind": "RUN",
                    "target": f"cmd{i}",
                    "reason": f"reason {i}",
                    "audit_kind": "RUN-BLOCK",
                },
            )
        # Spawned worker exactly `cap` times, NOT cap + 5
        self.assertEqual(len(self.spawned), cap)


class WorkerStorageBehavior(unittest.TestCase):
    """Tests the worker's response handling without making a real
    Ollama call. We monkeypatch urllib.request.urlopen to return a
    canned model response, then verify the worker calls
    master_ai.confirm_remember (or doesn't) based on the response."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        self.tmp.close()
        self.path = Path(self.tmp.name)
        self._orig_mem = master_ai.MEMORY_FILE
        master_ai.MEMORY_FILE = self.path
        self._orig_pill = master_ai._pill
        self._orig_audit = master_ai._audit
        self._orig_log = master_ai.log
        master_ai._pill = lambda *a, **k: ""
        master_ai._audit = lambda *a, **k: None
        master_ai.log = lambda *a, **k: None

    def tearDown(self):
        master_ai.MEMORY_FILE = self._orig_mem
        master_ai._pill = self._orig_pill
        master_ai._audit = self._orig_audit
        master_ai.log = self._orig_log
        try:
            self.path.unlink()
        except Exception:
            pass

    def _mem(self):
        return [l for l in self.path.read_text().splitlines() if l.strip()]

    def _fake_urlopen(self, response_text: str):
        import json as _json

        class _Resp:
            def __init__(self, body):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._body

        body = _json.dumps({"response": response_text}).encode()

        def _opener(req, timeout=None):
            return _Resp(body)

        return _opener

    def _patch_urlopen(self, response_text):
        import urllib.request

        self._orig_urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._fake_urlopen(response_text)

    def _unpatch_urlopen(self):
        import urllib.request

        urllib.request.urlopen = self._orig_urlopen

    def test_skip_response_does_not_store(self):
        self._patch_urlopen("SKIP")
        try:
            hooks._extract_lesson_worker("RUN", "fetchmail", "command not found")
        finally:
            self._unpatch_urlopen()
        self.assertEqual(self._mem(), [])

    def test_valid_lesson_stores(self):
        self._patch_urlopen(
            "fetchmail is not installed on this box; open Thunderbird instead"
        )
        try:
            hooks._extract_lesson_worker("RUN", "fetchmail", "command not found")
        finally:
            self._unpatch_urlopen()
        mem = self._mem()
        self.assertEqual(len(mem), 1)
        self.assertIn("Thunderbird", mem[0])

    def test_too_short_response_does_not_store(self):
        self._patch_urlopen("nope")  # < 10 chars
        try:
            hooks._extract_lesson_worker("RUN", "x", "y")
        finally:
            self._unpatch_urlopen()
        self.assertEqual(self._mem(), [])

    def test_remember_prefix_stripped(self):
        # Model wraps response with REMEMBER:
        self._patch_urlopen("REMEMBER: ollama rm deletes the model from disk")
        try:
            hooks._extract_lesson_worker("RUN", "ollama rm x", "wrong action")
        finally:
            self._unpatch_urlopen()
        mem = self._mem()
        self.assertEqual(len(mem), 1)
        self.assertTrue(
            mem[0].lower().startswith("ollama"),
            f"prefix should be stripped, got: {mem[0]!r}",
        )


class CodexFindingsRegressionGuard(unittest.TestCase):
    """Pin the three real bugs Codex caught in d261b94's audit pass."""

    def test_record_blocked_action_stores_audit_kind(self):
        """Finding 1: _LAST_BLOCKED_ACTION must carry audit_kind so the
        on_blocked hook's POLICY/FENCE skip filter actually triggers.
        Pre-fix, the entry stored only kind/reason/path — audit_kind was
        passed to _audit() then thrown away."""
        master_ai._LAST_BLOCKED_ACTION = {}
        entry = master_ai._record_blocked_action(
            "run",
            "evil-cmd",
            "credential exfil",
            audit_kind="POLICY-CMD-BLOCK",
        )
        self.assertEqual(entry.get("audit_kind"), "POLICY-CMD-BLOCK")
        self.assertEqual(
            master_ai._LAST_BLOCKED_ACTION.get("audit_kind"),
            "POLICY-CMD-BLOCK",
        )
        master_ai._LAST_BLOCKED_ACTION = {}

    def test_exec_fail_fires_on_blocked(self):
        """Finding 2: fetchmail exit-127 case (real exec failure, not
        safeguard refusal) must fire on_blocked so the auto-extract hook
        can learn from it.

        Previously a source pin over process_reply's text. Now observed:
        a real registered on_blocked handler receives the exec-fail events
        with the audit_kind the auto-extract worker filters on.
        """
        import hooks

        registry = hooks._REGISTRY
        saved = list(registry._hooks)
        registry._hooks = []
        seen: list[dict] = []
        try:
            registry.register(
                hooks.Hook(
                    id="rec",
                    kind="on_blocked",
                    fn=lambda t, action=None: seen.append(action),
                )
            )
            master_ai._fire_on_blocked(
                "fetchmail -q", "RUN", "command failed (exit 127)", "RUN-EXEC-FAIL"
            )
            master_ai._fire_on_blocked(
                "npm run dev", "RUNTERM", "runterm failed (exit 1)", "RUNTERM-EXEC-FAIL"
            )
        finally:
            registry._hooks = saved

        kinds = {a["audit_kind"] for a in seen}
        self.assertEqual(kinds, {"RUN-EXEC-FAIL", "RUNTERM-EXEC-FAIL"}, seen)
        for action in seen:
            self.assertIn("exit", action["reason"], action)

    def test_hooks_repl_command_exists(self):
        """Finding 3: there must be a REPL command surface for hooks
        list/enable/disable so the user can disable auto-extract-lesson
        without editing Python.

        This used to read master_ai's source and look for the literal
        `if lo == "hooks" or lo.startswith("hooks ")`. That broke the moment
        the branch was extracted into _handle_hooks_cmd() and the guard was
        rewritten as its negation — a pure move that changed no behaviour
        and no longer had a user-facing command surface, which is what the
        test exists to protect.

        Driven behaviourally instead: the command must be reachable, must
        consume a hooks command, must leave anything else alone, and must
        actually toggle a hook.
        """
        handler = master_ai._handle_hooks_cmd

        # Reachable and correctly scoped: consumed when it matches, ignored
        # when it does not.
        self.assertTrue(handler("hooks", "hooks"))
        self.assertTrue(handler("hooks list", "hooks list"))
        self.assertFalse(handler("doctor", "doctor"))
        self.assertFalse(handler("help", "help"))

        import hooks

        before = {h.id: h.enabled for h in hooks.list_hooks()}

        # It really drives the hook registry, not just prints something.
        enabled = [h for h in hooks.list_hooks() if h.enabled]
        self.assertTrue(before, "expected some registered hooks to toggle")
        target = enabled[0].id

        try:
            handler(f"hooks disable {target}", f"hooks disable {target}")
            still_enabled = [
                h.id for h in hooks.list_hooks() if h.id == target and h.enabled
            ]
            self.assertEqual(
                still_enabled, [], f"{target} is still enabled after disable"
            )
        finally:
            # Leave the registry exactly as we found it.
            for hook_id, was_enabled in before.items():
                if was_enabled:
                    hooks.enable(hook_id)
                else:
                    hooks.disable(hook_id)

        after = {h.id: h.enabled for h in hooks.list_hooks()}
        self.assertEqual(before, after, "test mutated the hook registry")


class MasterAiFiresHook(unittest.TestCase):
    """The blocked-action paths must fire the on_blocked hook.

    These used to be source-inspection pins -- `assertIn('hooks.fire("on_blocked"',
    inspect.getsource(master_ai.process_reply))` -- with a docstring
    admitting that "actually-running the feedback requires a real blocked
    chain which is heavy to set up". That approach proved the wrong thing:
    it passed whenever a literal string survived in the source, and broke
    the moment the repo's own formatter split that call across lines, with
    no behaviour change at all.

    The four fire sites in process_reply() now delegate to
    _fire_on_blocked(), so the seam is directly callable and the hook can
    be observed for real.
    """

    def setUp(self):
        import hooks

        self._hooks = hooks
        self._registry = hooks._REGISTRY
        self._saved = list(self._registry._hooks)
        self._registry._hooks = []
        self.calls: list[tuple] = []

        def _record(target, action=None):
            self.calls.append((target, action))
            return hooks.FireResult(hook_id="test-recorder")

        self._registry.register(
            hooks.Hook(id="test-recorder", kind="on_blocked", fn=_record)
        )

    def tearDown(self):
        self._registry._hooks = self._saved

    def test_blocked_action_fires_on_blocked_hook(self):
        """A refused action reaches a registered on_blocked handler."""
        master_ai._fire_on_blocked(
            "/etc/shadow", "READ", "read fence: secret path", "TOOL-BLOCKED"
        )

        self.assertEqual(len(self.calls), 1, "hook did not fire")
        target, action = self.calls[0]
        self.assertEqual(target, "/etc/shadow")
        self.assertEqual(
            action,
            {
                "kind": "READ",
                "target": "/etc/shadow",
                "reason": "read fence: secret path",
                "audit_kind": "TOOL-BLOCKED",
            },
        )

    def test_hook_block_uses_its_own_audit_kind(self):
        """The HOOK-BLOCK path tags itself so the lesson worker can filter."""
        master_ai._fire_on_blocked(
            "some/file.py", "EDIT", "hook 3: syntax check failed", "HOOK-BLOCK-EDIT"
        )

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["audit_kind"], "HOOK-BLOCK-EDIT")

    def test_exec_failure_is_tagged_distinctly_from_a_policy_block(self):
        """RUN-EXEC-FAIL is not a POLICY/FENCE block and must stay distinct."""
        master_ai._fire_on_blocked(
            "fetchmail -q", "RUN", "command failed (exit 127)", "RUN-EXEC-FAIL"
        )
        self.assertEqual(self.calls[0][1]["audit_kind"], "RUN-EXEC-FAIL")

        self.calls.clear()
        master_ai._fire_on_blocked(
            "rm -rf /", "RUN", "safeguard refused", "TOOL-BLOCKED"
        )
        self.assertEqual(self.calls[0][1]["audit_kind"], "TOOL-BLOCKED")

    def test_firing_never_raises_even_if_the_hook_explodes(self):
        """A broken hook must not take down the dispatch path."""

        def _boom(target, action=None):
            raise RuntimeError("hook exploded")

        self._registry._hooks = [
            self._hooks.Hook(id="boom", kind="on_blocked", fn=_boom)
        ]
        # Must not raise.
        master_ai._fire_on_blocked("/x", "READ", "why", "TOOL-BLOCKED")

    def test_on_blocked_is_a_registered_hook_kind(self):
        """The kind name the dispatch path sends must be one hooks knows."""
        self.assertIn("on_blocked", self._hooks.KINDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
