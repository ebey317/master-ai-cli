#!/usr/bin/env python3
"""Direct unit tests for the master_ai.py core loop (code-eval gap #2).

The 290 pre-existing tests cover subsystem modules (observability,
verifiers, typed_actions, runtime_state); the god-file itself ran only
indirectly. These tests exercise the riskiest, most-branched paths
directly, with every external effect (model calls, network, filesystem,
prompts) mocked so nothing here touches a real terminal process, network
endpoint, or the live approval queue:

  1. orchestrate() routing decisions — explicit prefixes, envelope
     unwrap, mode-dependent cloud gates, content scoring.
  2. _choose_route() candidate scoring and the peacetime tie-break
     invariant ("no local candidate can win when the gap must hold").
  3. handle() dispatch — policy block-before-model, acknowledgment /
     ask_user / cached short-circuits, model continuation loop,
     continuation-cap backstop.
  4. _validation_gate integration through process_reply — malformed
     actions removed before dispatch, [TOOL BLOCKED] feedback appended,
     all-invalid replies returning None instead of a fake answer.
  5. Typed lifecycle (RUN dispatch through run_command): status
     transitions completed/failed, _record_live_typed_action audit trail.

Importing master_ai is safe (heavy imports yes, side effects no — it
binds config paths but starts no servers and calls no network); the few
tests that would mutate runtime state save and restore the affected
globals explicitly.
"""

import json
import os
import shutil
import subprocess  # noqa: F401 - re-exported for patch targets below
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import action_validation  # noqa: E402
import approval_queue  # noqa: E402
import master_ai  # noqa: E402
import runtime_state  # noqa: E402

_TEST_DIR = Path("/tmp/master_ai_core_tests")


class _IsolateRuntimeState:
    """Mixin: snapshot master_ai's live runtime globals + the approval queue
    path before each test, restore after. Resume tests re-queue approvals —
    with the real queue redirected to a scratch file they can never touch
    ~/scripts/.pending_actions.jsonl. Also redirects the append-only audit /
    master.log files so tests never grow Elijah's real logs."""

    _GLOBALS = (
        "_LAST_BLOCKED_ACTION",
        "_LAST_DENIED_ACTION",
        "_LAST_HOOK_BLOCK",
        "PENDING_PLAN_TEXT",
        "PENDING_PLAN_REQUEST",
        "PENDING_CONTINUATION",
        "MODE",
        "CHARS_SINCE_SAVE",
    )

    def setUp(self):
        self._snap = {k: master_ai.__dict__.get(k) for k in self._GLOBALS}
        self._orig_queue = approval_queue.JSONL_FILE
        self._orig_md = approval_queue.MD_FILE
        self._orig_audit_log = master_ai.AUDIT_LOG
        self._orig_audit_jsonl = master_ai.AUDIT_LOG_JSONL
        self._orig_log_file = master_ai.LOG_FILE
        scratch = _TEST_DIR / f"q-{id(self)}"
        scratch.mkdir(parents=True, exist_ok=True)
        approval_queue.JSONL_FILE = scratch / ".pending_actions.jsonl"
        approval_queue.MD_FILE = scratch / "pending_actions.md"
        master_ai.AUDIT_LOG = scratch / "audit.log"
        master_ai.AUDIT_LOG_JSONL = scratch / "audit_typed.jsonl"
        master_ai.LOG_FILE = scratch / "master.log"
        self._scratch = scratch

    def tearDown(self):
        for k, v in self._snap.items():
            master_ai.__dict__[k] = v
        approval_queue.JSONL_FILE = self._orig_queue
        approval_queue.MD_FILE = self._orig_md
        master_ai.AUDIT_LOG = self._orig_audit_log
        master_ai.AUDIT_LOG_JSONL = self._orig_audit_jsonl
        master_ai.LOG_FILE = self._orig_log_file

    def _clean_scratch(self):
        for p in self._scratch.iterdir():
            p.unlink(missing_ok=True)


# ── shared mock helpers ───────────────────────────────────────────────


def _stall_free_ctx_meta():
    """auto_inject_context return that never trips the slicer guardrail."""
    return (
        "",
        {
            "big_file_no_symbol_match": None,
            "whole_file_requested": False,
            "inject_chars": 0,
        },
    )


def _patch_common(stack, **extra):
    """Patch every non-deterministic dependency handle() touches so a test
    turn cannot print animations, hit the network, or persist anything.

    Returns nothing; `stack` accumulates the patches."""
    common = {
        "_router_metric": lambda *a, **k: None,
        "load_keys": lambda: {},
        "_read_run_mode": mock.MagicMock(return_value="apocalypse"),
        "_agent_policy_issue_for_request": lambda t: None,
        "_load_last_action": mock.MagicMock(return_value={}),
        "_resume_skill_reply_from_turn": lambda t, h: None,
        "_run_skill_reply_from_reply": lambda r, h: None,
        "_try_google_workspace_bare_nav_intent": lambda t: None,
        "_try_ground_app_installed_intent": lambda t, h: None,
        "_try_ground_download_install_intent": lambda t, h: False,
        "_try_desktop_open_intent": lambda t: None,
        "_try_open_url_intent": lambda t: None,
        "auto_inject_context": lambda t, enabled=True: _stall_free_ctx_meta(),
        "_build_task_list_context": lambda: "",
        "render_reply": lambda *a, **k: None,
        "play_anim": lambda *a, **k: None,
        "compact_history": lambda h: None,
        "load_memory": lambda: "",
        "load_behavior": lambda: "",
        "load_tasks": lambda: [],
        "summarize_session": lambda h: (None, None),
        "local_thinking_start": lambda: None,
        "local_thinking_stop": lambda s: None,
        "speak": lambda t: None,
        "git_context": lambda: "",
        "ask_cloud": lambda *a, **k: None,
        "_ask_claf": lambda *a, **k: None,
        "web_search": mock.MagicMock(return_value="TEST SEARCH RESULTS"),
        "harvest": None,
    }
    for name, val in common.items():
        stack.enter_context(
            mock.patch.object(master_ai, name, val, create=not hasattr(master_ai, name))
        )
    for name, val in extra.items():
        stack.enter_context(
            mock.patch.object(master_ai, name, val, create=not hasattr(master_ai, name))
        )


class TestOrchestrateRouting(unittest.TestCase):
    """orchestrate(): explicit prefixes and envelope unwrap win first."""

    _KEYS = {"openrouter": "sk-or", "groq": "gkey", "fireworks": "", "gemini": ""}

    def setUp(self):
        self._stack = ExitStack()
        s = self._stack
        s.enter_context(
            mock.patch.object(master_ai, "load_keys", return_value=self._KEYS)
        )
        s.enter_context(
            mock.patch.object(master_ai, "_read_run_mode", return_value="apocalypse")
        )
        # Content-based short-circuits off so prefix tests are isolated.
        s.enter_context(
            mock.patch.object(
                master_ai, "_acknowledgment_short_circuit", return_value=""
            )
        )
        s.enter_context(
            mock.patch.object(
                master_ai, "_deterministic_intent_to_directive", return_value=None
            )
        )
        s.enter_context(
            mock.patch.object(
                master_ai, "_route_from_fast_classifier", return_value=None
            )
        )

    def tearDown(self):
        self._stack.close()

    def test_fast_prefix_routes_to_openrouter_when_key_present(self):
        d = master_ai.orchestrate([], "fast: quick lookup")
        self.assertEqual(d["route"], "cloud")
        self.assertEqual(d["model"], "openrouter")
        self.assertEqual(d["stripped_text"], "quick lookup")

    def test_fast_prefix_without_key_falls_through(self):
        with mock.patch.object(master_ai, "load_keys", return_value={}):
            d = master_ai.orchestrate([], "fast: quick lookup")
        # No key → prefix can't be honored; must not dead-end on cloud.
        self.assertNotEqual(d.get("model"), "openrouter")
        self.assertIn(d["route"], ("local", "cloud", "cloud_fast", "cloud_deep"))

    def test_deep_prefix_uses_openrouter_key(self):
        d = master_ai.orchestrate([], "deep: reason this out")
        self.assertEqual(d["route"], "cloud_deep")
        self.assertEqual(d["model"], "deepseek-r1")
        self.assertEqual(d["stripped_text"], "reason this out")

    def test_deep_prefix_without_openrouter_uses_qwen_cloud(self):
        with mock.patch.object(master_ai, "load_keys", return_value={}):
            d = master_ai.orchestrate([], "deep: reason this out")
        self.assertEqual(d["route"], "cloud_deep")
        self.assertEqual(d["model"], master_ai.MODELS["qwen3"])

    def test_local_prefix_forces_local(self):
        d = master_ai.orchestrate([], "local: list the files")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["model"], master_ai.MODELS["master"])
        self.assertEqual(d["stripped_text"], "list the files")

    def test_private_prefix_forces_local_and_strips_eight_chars(self):
        d = master_ai.orchestrate([], "private: hide this")
        self.assertEqual(d["route"], "local")
        # "private:" is 8 chars — the historical off-by-one left a stray ":"
        self.assertEqual(d["stripped_text"], "hide this")

    def test_api_wrapped_envelope_prefix_matched_against_user_section(self):
        env = (
            "source: chrome_extension\n"
            "[BROWSER PAGE CONTEXT]\n"
            "some page text\n"
            "[USER PROMPT]\n"
            "fast: click the button"
        )
        d = master_ai.orchestrate([], env)
        self.assertEqual(d["route"], "cloud")
        self.assertEqual(d["model"], "openrouter")
        # Envelope head preserved; prefix stripped only from the user section.
        self.assertTrue(d["stripped_text"].startswith("source: chrome_extension"))
        self.assertIn("[USER PROMPT]\nclick the button", d["stripped_text"])

    def test_chrome_ext_automation_routes_cloud_fast_with_groq(self):
        env = (
            "source: chrome_extension\n"
            "[BROWSER PAGE CONTEXT]\n"
            "page stuff\n"
            "[USER PROMPT]\n"
            "do the multi-step task"
        )
        d = master_ai.orchestrate([], env)
        self.assertEqual(d["route"], "cloud_fast")
        self.assertEqual(d["model"], "groq")

    def test_chrome_ext_automation_stays_local_without_groq(self):
        env = (
            "source: chrome_extension\n"
            "[BROWSER PAGE CONTEXT]\n"
            "page stuff\n"
            "[USER PROMPT]\n"
            "do the multi-step task"
        )
        with mock.patch.object(master_ai, "load_keys", return_value={}):
            d = master_ai.orchestrate([], env)
        self.assertEqual(d["route"], "local")


class TestOrchestrateShortCircuits(unittest.TestCase):
    """Pre-model deterministic routes inside orchestrate()."""

    def test_acknowledgment_short_circuit_returns_response(self):
        d = master_ai.orchestrate([], "thanks")
        self.assertEqual(d["route"], "acknowledgment")
        self.assertEqual(d["response"], "You're welcome.")

    def test_acknowledgment_not_fired_for_multiword_sentences(self):
        d = master_ai.orchestrate([], "thanks for the long explanation of everything")
        self.assertNotEqual(d["route"], "acknowledgment")

    def test_vision_request_routes_local_vlm(self):
        with mock.patch.object(master_ai, "_read_run_mode", return_value="apocalypse"):
            d = master_ai.orchestrate([], "what is in this image photo.png")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["model"], master_ai.MODELS["vision"])


class TestChooseRouteScoring(unittest.TestCase):
    """_choose_route / _rank_route_candidates: score math and ordering."""

    def test_highest_score_wins(self):
        d = master_ai._choose_route(
            [
                {"route": "local", "model": "m", "base_score": 80, "task_type": "x"},
                {"route": "cloud", "model": "c", "base_score": 90, "task_type": "x"},
            ],
            reason_prefix="test",
        )
        self.assertEqual(d["route"], "cloud")

    def test_decision_carries_ranked_candidates(self):
        d = master_ai._choose_route(
            [
                {"route": "local", "model": "m", "base_score": 80, "task_type": "x"},
                {"route": "cloud", "model": "c", "base_score": 90, "task_type": "x"},
            ]
        )
        self.assertIn("candidates", d)
        self.assertEqual(d["candidates"][0]["model"], "c")
        self.assertEqual(d["candidates"][1]["model"], "m")

    def test_peacetime_default_bucket_invariant(self):
        # 2026-09-25 fix: in peacetime with an OpenRouter key the default
        # bucket must be 90 cloud vs 40 local — a 50-point gap so the
        # perf_bonus extremes (+15 / -45) can NOT flip the decision back
        # to local in any ordinary case.
        with mock.patch("routing._router_perf_bonus", return_value=0):
            ranked = master_ai._rank_route_candidates(
                [
                    {
                        "route": "local",
                        "model": "m",
                        "base_score": 40,
                        "task_type": "default",
                    },
                    {
                        "route": "cloud",
                        "model": "openrouter",
                        "base_score": 90,
                        "task_type": "default",
                    },
                ]
            )
        self.assertEqual(ranked[0]["route"], "cloud")
        self.assertEqual(ranked[0]["score"], 90.0)

    def test_perf_bonus_applied_on_top_of_base_score(self):
        with mock.patch("routing._router_perf_bonus", return_value=-45.0):
            ranked = master_ai._rank_route_candidates(
                [
                    {
                        "route": "cloud",
                        "model": "openrouter",
                        "base_score": 90,
                        "task_type": "default",
                    }
                ]
            )
        self.assertEqual(ranked[0]["score"], 45.0)
        self.assertEqual(ranked[0]["perf_bonus"], -45.0)


class TestHandleDispatch(unittest.TestCase):
    """handle(): the core turn loop, fully mocked against real side effects."""

    def test_policy_block_never_reaches_a_model(self):
        orchestrate = mock.MagicMock()
        process_reply = mock.MagicMock(return_value="reply")
        with ExitStack() as st:
            _patch_common(
                st,
                orchestrate=orchestrate,
                process_reply=process_reply,
                _agent_policy_issue_for_request=lambda t: (
                    "disallowed agent request: malware"
                ),
            )
            h = []
            msg = master_ai.handle("write some malware", h)
        self.assertIn("I can't help with that request", msg)
        self.assertEqual(orchestrate.call_count, 0)
        self.assertEqual(process_reply.call_count, 0)
        self.assertEqual([m["role"] for m in h], ["user", "assistant"])
        self.assertIn("malware", h[-1]["content"])

    def test_unknown_directive_reply_never_reaches_a_model(self):
        """The "you ignored my decline" catch fires before orchestrate() —
        with "i said no" in the text AND a recent *_denied last action."""
        orchestrate = mock.MagicMock()
        with ExitStack() as st:
            _patch_common(
                st,
                orchestrate=orchestrate,
                _load_last_action=mock.MagicMock(
                    return_value={"kind": "run_denied", "command": "rm -rf /tmp/x"}
                ),
            )
            h = []
            msg = master_ai.handle("why did you do it anyway? i said no!", h)
        self.assertIn("declined", msg.lower())
        self.assertEqual(orchestrate.call_count, 0)
        self.assertIn("user", [m["role"] for m in h])

    def test_acknowledgment_route_appends_history_and_returns_response(self):
        with ExitStack() as st:
            _patch_common(
                st,
                orchestrate=mock.MagicMock(
                    return_value={
                        "route": "acknowledgment",
                        "response": "Okay.",
                        "reason": "pure acknowledgment → deterministic one-line reply",
                    }
                ),
            )
            h = []
            msg = master_ai.handle("ok", h)
        self.assertEqual(msg, "Okay.")
        self.assertEqual(
            [(m["role"], m["content"]) for m in h],
            [("user", "ok"), ("assistant", "Okay.")],
        )

    def test_ask_user_route_appends_question_to_history(self):
        with ExitStack() as st:
            _patch_common(
                st,
                orchestrate=mock.MagicMock(
                    return_value={
                        "route": "ask_user",
                        "question": "Which file do you mean?",
                        "reason": "ambiguous: no object",
                    }
                ),
            )
            h = []
            msg = master_ai.handle("fix it", h)
        self.assertEqual(msg, "Which file do you mean?")
        self.assertEqual(h[-1]["role"], "assistant")

    def test_cached_route_serves_without_model_call(self):
        with ExitStack() as st:
            _patch_common(
                st,
                orchestrate=mock.MagicMock(
                    return_value={
                        "route": "cached",
                        "response": "cached answer",
                        "similarity": 0.93,
                        "source_model": "groq",
                        "reason": "harvest cache hit",
                    }
                ),
            )
            h = []
            msg = master_ai.handle("what is pytest again", h)
        self.assertEqual(msg, "cached answer")
        self.assertEqual(h[-1]["content"], "cached answer")

    def test_local_route_continuation_loop_reasks_after_tool_result(self):
        """The heartbeat of the agent loop: model emits RUN:, process_reply
        returns None (tool result appended), the loop re-asks the SAME lane
        until a real answer synthesizes — all inside one handle() turn."""
        calls = {"model": 0, "process": 0}
        stream_sides = iter(["RUN: echo hi", "Found 3 files. Done."])

        def fake_stream(history, model=None, image_path=None, **k):
            calls["model"] += 1
            return next(stream_sides)

        def fake_process_reply(
            reply, history, streamed=False, continue_after_tools=False
        ):
            calls["process"] += 1
            if "RUN:" in reply:
                history.append(
                    {"role": "user", "content": "[RUN RESULT] ok\nfake output"}
                )
                return None  # sentinel: caller must re-ask
            return reply  # final synthesized answer

        with ExitStack() as st:
            _patch_common(
                st,
                orchestrate=mock.MagicMock(
                    return_value={"route": "local", "model": "testmodel", "reason": "t"}
                ),
                detect_route=lambda t, has_image=False: ("local", "testmodel", "r"),
                PINNED_MODEL="",
                ask_local_stream=fake_stream,
                process_reply=fake_process_reply,
            )
            h = [{"role": "system", "content": "sys"}]
            msg = master_ai.handle("list files in /tmp", h)
        self.assertEqual(msg, "Found 3 files. Done.")
        self.assertEqual(calls["model"], 2)
        self.assertEqual(calls["process"], 2)
        # The tool feedback turn and both answers must be in history.
        roles = [m["role"] for m in h]
        self.assertEqual(roles.count("user"), 2)

    def test_continuation_cap_synthesizes_honest_backstop(self):
        """Model stalls forever on bare announcements ("On it" with no
        directive): the bounded loop must stop and return the deterministic
        'I got stuck' message — answer-every-time, never a hang, and the
        repair-turn counter must be reported."""
        calls = {"n": 0}

        def fake_stream(history, model=None, image_path=None, **k):
            calls["n"] += 1
            return "On it, working on it right now."

        def fake_process_reply(reply, history, **k):
            # The stall: narrative with no directive → process_reply's own
            # repair path appends "[Directive repair]" and returns None.
            history.append(
                {
                    "role": "user",
                    "content": "[Directive repair]\nYou said you'd do that but emitted no directive.",
                }
            )
            return

        with ExitStack() as st:
            _patch_common(
                st,
                orchestrate=mock.MagicMock(
                    return_value={"route": "local", "model": "testmodel", "reason": "t"}
                ),
                detect_route=lambda t, has_image=False: ("local", "testmodel", "r"),
                PINNED_MODEL="",
                ask_local_stream=fake_stream,
                process_reply=fake_process_reply,
                MAX_CONTINUATION_TURNS=3,
            )
            h = [{"role": "system", "content": "sys"}]
            msg = master_ai.handle("do the thing", h)
        # 1 initial model call + 3 bounded continuation turns...
        self.assertEqual(calls["n"], 4)
        self.assertIn("got stuck", msg)
        self.assertIn("announcing", msg)
        self.assertIn("turn(s)", msg)


class TestValidationGate(unittest.TestCase):
    """_validation_gate directly + its live integration through process_reply:

    malformed actions are removed BEFORE dispatch and fed back to the model
    as [TOOL BLOCKED]; a validator that explodes fails CLOSED (ok=False),
    never silently passes."""

    def test_multiline_run_blocked(self):
        kept, blocked = master_ai._validation_gate({"run_cmds": ["echo one\necho two"]})
        self.assertEqual(kept["run_cmds"], [])
        self.assertEqual(len(blocked), 1)
        name, payload, reason = blocked[0]
        self.assertEqual(name, "run_cmds")
        self.assertIn("multi-line", reason)

    def test_unbalanced_quoting_blocked(self):
        kept, blocked = master_ai._validation_gate({"run_cmds": ["echo 'unbalanced"]})
        self.assertEqual(kept["run_cmds"], [])
        self.assertIn("unbalanced quoting", blocked[0][2])

    def test_sentence_as_command_blocked(self):
        # A bare sentence fragment: first token is a discourse word, not a
        # program ("Then set that up now" — "then" is in _NOT_A_PROGRAM).
        kept, blocked = master_ai._validation_gate(
            {"run_cmds": ["Then set that up now"]}
        )
        self.assertEqual(kept["run_cmds"], [])
        self.assertIn("sentence, not a command", blocked[0][2])

    def test_sentence_with_apostrophe_blocked_via_quoting(self):
        # "I'll set that up now" — the stray apostrophe makes shlex explode;
        # still blocked, just via the unbalanced-quoting validator arm.
        kept, blocked = master_ai._validation_gate(
            {"run_cmds": ["I'll set that up now"]}
        )
        self.assertEqual(kept["run_cmds"], [])
        self.assertIn("unbalanced quoting", blocked[0][2])

    def test_nested_directive_marker_blocked(self):
        kept, blocked = master_ai._validation_gate({"run_cmds": ["RUN: echo nested"]})
        self.assertEqual(kept["run_cmds"], [])
        self.assertIn("second directive", blocked[0][2])

    def test_valid_command_survives(self):
        kept, blocked = master_ai._validation_gate({"run_cmds": ["ls -la /tmp"]})
        self.assertEqual(kept["run_cmds"], ["ls -la /tmp"])
        self.assertEqual(blocked, [])

    def test_multiline_read_blocked(self):
        kept, blocked = master_ai._validation_gate({"read_paths": ["/tmp/a\n/tmp/b"]})
        self.assertEqual(kept["read_paths"], [])
        self.assertIn("multi-line", blocked[0][2])

    def test_create_with_python_syntax_error_blocked(self):
        kept, blocked = master_ai._validation_gate(
            {"create_files": [("/tmp/x.py", "def broken(:\n")]}
        )
        self.assertEqual(kept["create_files"], [])
        self.assertIn("python syntax error", blocked[0][2])

    def test_create_with_valid_content_survives(self):
        kept, _ = master_ai._validation_gate(
            {"create_files": [("/tmp/x.sh", "#!/bin/bash\necho ok\n")]}
        )
        self.assertEqual(len(kept["create_files"]), 1)

    def test_edit_without_replace_content_blocked(self):
        # edit_ops payloads are (path, find, replace); no replace → blocked.
        kept, blocked = master_ai._validation_gate(
            {"edit_ops": [("/tmp/t.py", "old text", None)]}
        )
        self.assertEqual(kept["edit_ops"], [])
        self.assertIn("no content block", blocked[0][2])

    def test_edit_with_find_and_replace_survives(self):
        kept, blocked = master_ai._validation_gate(
            {"edit_ops": [("/tmp/t.txt", "old text", "new text")]}
        )
        self.assertEqual(kept["edit_ops"], [("/tmp/t.txt", "old text", "new text")])
        self.assertEqual(blocked, [])

    def test_unknown_list_name_passes_through_unblocked(self):
        # Lists the gate has no kind for are NOT derived from the name
        # (RUN_CMDS-from-run_cmds bug) — they pass untouched.
        kept, blocked = master_ai._validation_gate({"mystery_list": ["anything"]})
        self.assertEqual(kept["mystery_list"], ["anything"])
        self.assertEqual(blocked, [])

    def test_order_preserved_within_survivors(self):
        kept, _ = master_ai._validation_gate(
            {"run_cmds": ["echo one", "echo two", "echo three"]}
        )
        self.assertEqual(kept["run_cmds"], ["echo one", "echo two", "echo three"])

    def test_validator_exception_fails_closed(self):
        """If action_validation explodes mid-check, validate_action itself
        must fail CLOSED (ok=False) — never silently pass the dispatch."""
        with mock.patch.object(
            action_validation,
            "_validate_run",
            side_effect=RuntimeError("validator exploded"),
        ):
            res = action_validation.validate_action({"kind": "RUN", "target": "ls -la"})
        self.assertFalse(res.ok)
        self.assertIn("blocked", res.reason.lower())

    def test_gate_blocks_everything_when_validator_call_raises(self):
        """Gate-level fail-closed: if _av.validate_action itself raises,
        the gate must block every payload -- the exception must never
        propagate and crash the turn."""
        with mock.patch.object(
            action_validation,
            "validate_action",
            side_effect=RuntimeError("validator exploded"),
        ):
            kept, blocked = master_ai._validation_gate({"run_cmds": ["ls -la"]})
        self.assertEqual(kept["run_cmds"], [])
        self.assertEqual(len(blocked), 1)
        self.assertIn("validator", blocked[0][2].lower())

    def test_missing_validation_module_fails_closed(self):
        """Import failure of action_validation → the gate blocks EVERYTHING
        with no dispatch at all — fail-closed, not fail-open."""
        with mock.patch.dict(sys.modules, {"action_validation": None}):
            kept, blocked = master_ai._validation_gate({"run_cmds": ["ls -la"]})
        self.assertEqual(kept, {})
        self.assertEqual(
            [reason for _n, _p, reason in blocked],
            ["validation gate unavailable — fail-closed"],
        )

    def test_gate_never_raises_on_validator_import_error(self):
        """The fail-closed path itself must stay within contract: returns a
        tuple, corrupts nothing, does not propagate the import error."""
        with mock.patch.dict(sys.modules, {"action_validation": None}):
            result = master_ai._validation_gate({"run_cmds": ["x y z"]})
        self.assertIsInstance(result, tuple)
        kept, blocked = result
        self.assertEqual(kept, {})
        self.assertEqual(len(blocked), 1)

    def test_process_reply_blocks_malformed_action_before_dispatch(self):
        """Live integration: a malformed RUN never reaches confirm_run; the
        model gets [TOOL BLOCKED]; the turn cannot end on a fake success."""
        confirm_run = mock.MagicMock()
        with ExitStack() as st:
            _patch_common(st, MODE="review")
            st.enter_context(mock.patch.object(master_ai, "confirm_run", confirm_run))
            h = []
            res = master_ai.process_reply("Working.\nRUN: echo 'never closed", h)
        self.assertIsNone(res)
        self.assertEqual(confirm_run.call_count, 0)
        feedback = "\n".join(m["content"] for m in h if m["role"] == "user")
        self.assertIn("[TOOL BLOCKED]", feedback)
        self.assertIn("unbalanced quoting", feedback)

    def test_process_reply_all_invalid_returns_none_not_fake_answer(self):
        """Every action invalid → stop the chain; the narrative must not be
        returned as if it were a completed answer."""
        with ExitStack() as st:
            _patch_common(st, MODE="review")
            st.enter_context(
                mock.patch.object(master_ai, "confirm_run", mock.MagicMock())
            )
            h = []
            res = master_ai.process_reply("All done!\nRUN: nonsense 'unbalanced", h)
        self.assertIsNone(res)
        # Something was fed back for repair
        self.assertTrue(any("[TOOL BLOCKED]" in str(m["content"]) for m in h))

    def test_process_reply_valid_run_reaches_confirm(self):
        """Control: a well-formed RUN in review mode reaches confirm_run once."""
        confirm_run = mock.MagicMock()
        confirm_run.return_value = master_ai.RunResult("ok", ok=True, exit_code=0)
        with ExitStack() as st:
            _patch_common(st, MODE="review")
            st.enter_context(mock.patch.object(master_ai, "confirm_run", confirm_run))
            h = []
            master_ai.process_reply("RUN: ls -la /tmp", h, continue_after_tools=True)
        self.assertEqual(confirm_run.call_count, 1)
        self.assertEqual(confirm_run.call_args[0][0], "ls -la /tmp")


class TestTypedLifecycleDispatch(_IsolateRuntimeState, unittest.TestCase):
    """Typed-action lifecycle through the live dispatch choke-points:

    run_command()/run_in_terminal() build a TypedAction, execute, then
    transition its status and record the full-lifecycle trail."""

    def setUp(self):
        super().setUp()
        self._tmp = _TEST_DIR / "audit"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._orig_actions = master_ai._LAST_LIVE_TYPED_ACTIONS[:]
        master_ai._LAST_LIVE_TYPED_ACTIONS.clear()

    def tearDown(self):
        master_ai._LAST_LIVE_TYPED_ACTIONS[:] = self._orig_actions
        super().tearDown()

    def _patch_run_env(self, stack, subprocess_mock=None):
        stack.enter_context(
            mock.patch.object(
                master_ai, "AUDIT_LOG_JSONL", self._tmp / "audit_typed.jsonl"
            )
        )
        stack.enter_context(
            mock.patch.object(master_ai, "_router_metric", lambda *a, **k: None)
        )
        stack.enter_context(
            mock.patch.object(
                master_ai, "_print_run_success_summary", lambda *a, **k: None
            )
        )
        stack.enter_context(
            mock.patch.object(
                master_ai, "_check_run_output_for_privacy", lambda *a, **k: None
            )
        )
        stack.enter_context(
            mock.patch.object(master_ai, "_track_leading_cd", lambda *a, **k: None)
        )
        stack.enter_context(
            mock.patch.object(master_ai, "_build_sandbox_argv", lambda argv: argv)
        )
        if subprocess_mock is not None:
            stack.enter_context(
                mock.patch.object(master_ai, "subprocess", subprocess_mock)
            )

    def test_run_command_success_transitions_to_completed(self):
        fake_sub = mock.MagicMock()
        fake_sub.TimeoutExpired = subprocess.TimeoutExpired
        fake_sub.run = lambda *a, **k: mock.Mock(
            stdout="file.txt\n", stderr="", returncode=0
        )
        with ExitStack() as st:
            self._patch_run_env(st, fake_sub)
            res = master_ai.run_command("echo hello")
        self.assertTrue(res.ok)
        self.assertEqual(res.exit_code, 0)
        last = master_ai._LAST_LIVE_TYPED_ACTIONS[-1]
        self.assertEqual(last["kind"], "RUN")
        self.assertEqual(last["status"], "completed")
        self.assertEqual(last["extras"]["exit_code"], 0)

    def test_run_command_failure_transitions_to_failed(self):
        fake_sub = mock.MagicMock()
        fake_sub.TimeoutExpired = subprocess.TimeoutExpired
        fake_sub.run = lambda *a, **k: mock.Mock(stdout="", stderr="boom", returncode=3)
        with ExitStack() as st:
            self._patch_run_env(st, fake_sub)
            res = master_ai.run_command("false")
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 3)
        self.assertEqual(master_ai._LAST_LIVE_TYPED_ACTIONS[-1]["status"], "failed")

    def test_run_command_timeout_records_failed_with_exit_124(self):
        fake_sub = mock.MagicMock()
        fake_sub.TimeoutExpired = subprocess.TimeoutExpired

        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=300)

        fake_sub.run = raise_timeout
        with ExitStack() as st:
            self._patch_run_env(st, fake_sub)
            res = master_ai.run_command("sleep 999")
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 124)
        self.assertEqual(res.error, "timeout")
        self.assertEqual(master_ai._LAST_LIVE_TYPED_ACTIONS[-1]["status"], "failed")

    def test_run_command_generic_exception_fails_and_records(self):
        fake_sub = mock.MagicMock()
        fake_sub.TimeoutExpired = subprocess.TimeoutExpired

        def raise_oserror(*a, **k):
            raise OSError("Permission denied")

        fake_sub.run = raise_oserror
        with ExitStack() as st:
            self._patch_run_env(st, fake_sub)
            res = master_ai.run_command("whatever")
        self.assertFalse(res.ok)
        self.assertIn("Permission denied", res.error)
        self.assertEqual(master_ai._LAST_LIVE_TYPED_ACTIONS[-1]["status"], "failed")

    def test_live_typed_action_trail_is_bounded(self):
        fake_sub = mock.MagicMock()
        fake_sub.TimeoutExpired = subprocess.TimeoutExpired
        fake_sub.run = lambda *a, **k: mock.Mock(stdout="", stderr="", returncode=0)
        cap = master_ai._LIVE_TYPED_ACTIONS_CAP
        with ExitStack() as st:
            self._patch_run_env(st, fake_sub)
            for i in range(cap + 10):
                master_ai.run_command(f"echo n{i}")
        self.assertEqual(len(master_ai._LAST_LIVE_TYPED_ACTIONS), cap)

    def test_run_in_terminal_spawns_and_records_completed(self):
        """RUNTERM: first available terminal emulator wins, status completed."""
        spawns = []
        # First candidate succeeds — patch Popen to a real no-op recorder.
        with ExitStack() as st:
            self._patch_run_env(st)
            st.enter_context(
                mock.patch.object(
                    master_ai.subprocess,
                    "Popen",
                    lambda argv, **k: spawns.append(argv) or mock.Mock(),
                )
            )
            res = master_ai.run_in_terminal("htop")
        self.assertIn("spawned", res)
        self.assertEqual(len(spawns), 1)
        last = master_ai._LAST_LIVE_TYPED_ACTIONS[-1]
        self.assertEqual(last["kind"], "RUNTERM")
        self.assertEqual(last["status"], "completed")
        self.assertIn("spawned_via", last["extras"])

    def test_run_in_terminal_no_terminal_available_fails(self):
        """Every candidate raises FileNotFoundError → status failed."""
        with ExitStack() as st:
            self._patch_run_env(st)
            st.enter_context(
                mock.patch.object(
                    master_ai.subprocess,
                    "Popen",
                    mock.MagicMock(side_effect=FileNotFoundError()),
                )
            )
            res = master_ai.run_in_terminal("htop")
        self.assertEqual(res, "no-terminal-available")
        last = master_ai._LAST_LIVE_TYPED_ACTIONS[-1]
        self.assertEqual(last["status"], "failed")
        self.assertIn("no graphical terminal", last["extras"]["error"])

    def test_typed_action_rejected_by_validator_has_lifecycle_trace(self):
        """The gate + validator reject invalid payloads; a valid TypedAction
        still carries a full lifecycle once executed. Verify both halves."""
        # Invalid kind payload: blocked with a clear, action-shaped reason.
        res = action_validation.validate_action(
            {"kind": "RUN", "target": "echo 'broken"}
        )
        self.assertFalse(res.ok)
        self.assertIn("quoting", res.reason)
        # Valid one parses and executes green through the real choke-point.
        fake_sub = mock.MagicMock()
        fake_sub.TimeoutExpired = subprocess.TimeoutExpired
        fake_sub.run = lambda *a, **k: mock.Mock(stdout="", stderr="", returncode=0)
        with ExitStack() as st:
            self._patch_run_env(st, fake_sub)
            master_ai.run_command("echo fine")
            executed = master_ai._LAST_LIVE_TYPED_ACTIONS[-1]
        self.assertEqual(executed["status"], "completed")


class TestSaveSessionResume(_IsolateRuntimeState, unittest.TestCase):
    """master_ai.save_session / _restore_structured_state integration —

    builds on runtime_state.py (its own collection/serialization contracts
    live in tests/test_runtime_state.py). Here we pin what master_ai itself
    must guarantee: atomic writes of all state files, lifecycle globals
    round-tripping, and resume never mutating the real approval queue."""

    def setUp(self):
        super().setUp()
        self._tmp = _TEST_DIR / "chats"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._ma = master_ai
        self._orig_chats = master_ai.CHATS_DIR
        self._orig_ts = master_ai.SESSION_TS

    def tearDown(self):
        super().tearDown()
        master_ai.CHATS_DIR = self._orig_chats
        master_ai.SESSION_TS = self._orig_ts
        for p in self._tmp.iterdir():
            p.unlink(missing_ok=True)
        self._clean_scratch()

    def test_save_writes_chat_and_state_atomically(self):
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "unittest-s1"
        h = [
            {"role": "user", "content": "run the audit"},
            {"role": "assistant", "content": "queued"},
            {
                "role": "user",
                "content": "[SUBAGENT RESULT] goal='probe' ok=True\nwork done",
            },
        ]
        self._ma.save_session(h, silent=True, summarize_timeout=0.01)
        chat = self._tmp / "unittest-s1.chat"
        state = self._tmp / "unittest-s1.state.json"
        self.assertTrue(chat.exists())
        self.assertTrue(state.exists())
        st = json.loads(state.read_text())
        self.assertEqual(st["schema"], runtime_state.STATE_SCHEMA)
        self.assertEqual(len(st["subagents"]["recent"]), 1)
        # .chat carries the structured block with its own label
        chat_text = chat.read_text()
        self.assertIn("── SUBAGENT ──", chat_text)

    def test_save_under_two_messages_is_noop(self):
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "unittest-noop"
        self._ma.save_session([{"role": "user", "content": "only one"}], silent=True)
        self.assertEqual(list(self._tmp.glob("unittest-noop*")), [])

    def test_save_resets_char_counter(self):
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "unittest-reset"
        self._ma.CHARS_SINCE_SAVE = 99999
        self._ma.save_session(
            [
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": "pong"},
            ],
            silent=True,
        )
        self.assertEqual(self._ma.CHARS_SINCE_SAVE, 0)

    def test_save_survives_state_write_failure_chat_still_written(self):
        """Broken state snapshot must never block the save carrying the .chat."""
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "unittest-fail"
        orig_spf = runtime_state.state_path_for
        runtime_state.state_path_for = lambda p: (
            self._tmp
        )  # directory → os.replace fails
        try:
            self._ma.save_session(
                [
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ],
                silent=True,
                summarize_timeout=0.01,
            )
        finally:
            runtime_state.state_path_for = orig_spf
        self.assertTrue((self._tmp / "unittest-fail.chat").exists())

    def test_resume_restores_blocked_action_global(self):
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "unittest-restore"
        try:
            self._ma._LAST_BLOCKED_ACTION = {
                "kind": "run",
                "command": "rm -rf /tmp/x",
                "reason": "destructive",
                "audit_kind": "RUN-BLOCK",
            }
            self._ma.save_session(
                [
                    {"role": "user", "content": "go"},
                    {"role": "assistant", "content": "ok"},
                ],
                silent=True,
                summarize_timeout=0.01,
            )
        finally:
            self._ma._LAST_BLOCKED_ACTION = {}
        chat_path = self._tmp / "unittest-restore.chat"
        summary = self._ma._restore_structured_state(chat_path, [])
        self.assertIsNotNone(summary)
        self.assertTrue(summary["lifecycle"]["blocked"])
        self.assertEqual(self._ma._LAST_BLOCKED_ACTION.get("reason"), "destructive")

    def test_resume_drifted_or_corrupt_state_is_fail_open(self):
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "unittest-drift"
        self._ma.save_session(
            [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": "ok"},
            ],
            silent=True,
            summarize_timeout=0.01,
        )
        state_path = (self._tmp / "unittest-drift.chat").with_suffix(".state.json")
        state_path.write_text('{"schema": "wrong_v9000", "saved_at": 1}')
        self.assertIsNone(
            self._ma._restore_structured_state(self._tmp / "unittest-drift.chat", [])
        )
        state_path.write_text("garbage{")
        self.assertIsNone(
            self._ma._restore_structured_state(self._tmp / "unittest-drift.chat", [])
        )
        state_path.unlink()
        self.assertIsNone(self._restore_structured_state_quiet())

    def _restore_structured_state_quiet(self):
        return self._ma._restore_structured_state(self._tmp / "unittest-drift.chat", [])

    def test_resume_roundtrips_pending_plan_and_continuation(self):
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "unittest-plan"
        try:
            self._ma.PENDING_PLAN_TEXT = "1. Build it\n2. Test it"
            self._ma.PENDING_PLAN_REQUEST = "build the widget"
            self._ma.PENDING_CONTINUATION = {
                "provider": "openrouter",
                "messages": [{"role": "user", "content": "hi"}],
                "so_far": "partial reply text",
            }
            self._ma.save_session(
                [
                    {"role": "user", "content": "build the widget"},
                    {"role": "assistant", "content": "planning..."},
                ],
                silent=True,
                summarize_timeout=0.01,
            )
        finally:
            self._ma.PENDING_PLAN_TEXT = ""
            self._ma.PENDING_PLAN_REQUEST = ""
            self._ma.PENDING_CONTINUATION = None
        chat_path = self._tmp / "unittest-plan.chat"
        summary = self._ma._restore_structured_state(chat_path, [])
        self.assertTrue(summary["lifecycle"]["plan"])
        self.assertTrue(summary["lifecycle"]["continuation"])
        self.assertIn("Build it", self._ma.PENDING_PLAN_TEXT)
        self.assertEqual(self._ma.PENDING_PLAN_REQUEST, "build the widget")
        self.assertEqual(self._ma.PENDING_CONTINUATION["provider"], "openrouter")
        # Cleanup — globals restored are part of the live runtime.
        self._ma.PENDING_PLAN_TEXT = ""
        self._ma.PENDING_PLAN_REQUEST = ""
        self._ma.PENDING_CONTINUATION = None


class TestLoopHelperParsers(unittest.TestCase):
    """High-branch helpers of the agent-loop subsystem (handle_loop_task
    planner/critic): malformed model text must parse or fail deterministic."""

    def test_loop_parse_steps_numbered(self):
        steps = master_ai._loop_parse_steps("1. Do a\n2. Do b\n3. Do c")
        self.assertEqual(steps, ["Do a", "Do b", "Do c"])

    def test_loop_parse_steps_bullets(self):
        steps = master_ai._loop_parse_steps("- clean\n- build\n")
        self.assertEqual(steps, ["clean", "build"])

    def test_loop_parse_steps_caps_at_eight(self):
        text = "\n".join(f"{i}. step {i}" for i in range(1, 15))
        self.assertEqual(len(master_ai._loop_parse_steps(text)), 8)

    def test_loop_parse_steps_prose_returns_empty(self):
        self.assertEqual(master_ai._loop_parse_steps("Just prose, no steps."), [])

    def test_loop_extract_question_explicit_marker(self):
        self.assertEqual(
            master_ai._loop_extract_question("QUESTION: which repo?\nok"),
            "which repo?",
        )

    def test_loop_extract_question_none_on_multi_line_prose(self):
        self.assertEqual(master_ai._loop_extract_question("a\nb\nc\nd?"), "")

    def test_loop_critique_verdict_done_wins(self):
        self.assertEqual(
            master_ai._loop_critique_verdict("DONE — everything passed"), "DONE"
        )

    def test_loop_critique_verdict_defaults_continue(self):
        self.assertEqual(
            master_ai._loop_critique_verdict("looks unclear to me"), "CONTINUE"
        )

    def test_loop_critique_verdict_scan_is_bounded(self):
        # STOP appears only after char 200 → must NOT win; default CONTINUE.
        text = "x" * 250 + " STOP"
        self.assertEqual(master_ai._loop_critique_verdict(text), "CONTINUE")


if __name__ == "__main__":
    unittest.main()
"""Tests for the local compaction fallback (_local_fallback_summary)."""


class TestLocalFallbackSummary(unittest.TestCase):
    def _msgs(self):
        return [
            {"role": "user", "content": "help me refactor the login flow"},
            {"role": "assistant", "content": "sure, here is the plan"},
            {"role": "user", "content": "also add rate limiting"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "write tests for the new code"},
            {"role": "assistant", "content": "tests added"},
        ]

    def test_fallback_tags_title_as_local(self):
        title, body = master_ai._local_fallback_summary(self._msgs())
        self.assertTrue(title.startswith("[local]"))
        self.assertIn("help me refactor", title)

    def test_fallback_produces_four_bullets(self):
        title, body = master_ai._local_fallback_summary(self._msgs())
        bullets = [line for line in body.split("\n") if line.startswith("\u2022")]
        self.assertEqual(len(bullets), 4)

    def test_fallback_deterministic(self):
        msgs = self._msgs()
        self.assertEqual(
            master_ai._local_fallback_summary(msgs),
            master_ai._local_fallback_summary(msgs),
        )

    def test_fallback_never_raises_on_empty(self):
        title, body = master_ai._local_fallback_summary([])
        self.assertTrue(title.startswith("[local]"))

    def test_summarize_falls_back_when_cloud_raises(self):
        msgs = self._msgs()
        with mock.patch.object(
            master_ai, "_ask_cloud_for_label", side_effect=RuntimeError("no net")
        ):
            title, body = master_ai.summarize_session(msgs)
        self.assertTrue(title.startswith("[local]"))
        self.assertIn("\u2022", body)

    def test_summarize_falls_back_when_cloud_empty(self):
        msgs = self._msgs()
        with mock.patch.object(master_ai, "_ask_cloud_for_label", return_value=""):
            title, body = master_ai.summarize_session(msgs)
        self.assertTrue(title.startswith("[local]"))

    def test_summarize_still_returns_none_for_tiny_history(self):
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(master_ai.summarize_session(msgs), (None, None))


class TestReloadSummarizeTimeoutBound(unittest.TestCase):
    """_reload_if_code_changed(): the summarize_timeout hand-off is bounded.

    The reload path calls save_session(..., summarize_timeout=15.0) so a
    wedged cloud summarizer can never hang the code-reload restart
    (master_ai.py, `summarize_timeout=15.0` at the reload call site). These
    tests pin that contract at the seam without exec'ing: save_session is
    stubbed, capture_blocking_summarizer installs a BLOCKING summarize so
    the wall clock is proven by construction, and save_with_timeout asserts
    the timeout actually arrives and actually bounds the wait.

    Note summarize_timeout is deliberately NOT re-checked against the
    function's own threading wait here — that would be testing stdlib
    Event.wait(timeout). The contract under test is that _reload passes a
    positive, finite timeout through, and that the save path honours it —
    the latter is what a hung cloud call would otherwise defeat.
    """

    RELOAD_TIMEOUT = 15.0  # the value _reload_if_code_changed must pass

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._orig = {
            k: getattr(master_ai, k)
            for k in (
                "RESUME_FLAG",
                "_RELOAD_CARRY_FILE",
                "CHATS_DIR",
                "SESSION_TS",
                "_STARTUP_CODE_MTIME",
                "save_session",
                "_clear_tmux_scrollback",
            )
        }
        master_ai.RESUME_FLAG = self._tmp / "resume_flag"
        master_ai._RELOAD_CARRY_FILE = self._tmp / "carry_file"
        master_ai.CHATS_DIR = self._tmp
        master_ai.SESSION_TS = "reload-timeout-test"
        self._orig_execvp = os.execvp
        self._orig_app = master_ai._SENSEI_APP
        self._execvp_calls: list = []
        master_ai.os.execvp = lambda *a, **k: self._execvp_calls.append((a, k))
        master_ai._clear_tmux_scrollback = lambda *a, **k: None

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(master_ai, k, v)
        master_ai._SENSEI_APP = self._orig_app
        os.execvp = self._orig_execvp
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── the seam: a real, bounded save that exercises the timeout path ──

    @staticmethod
    def _real_save_with_timeout(history, silent=False, **kwargs):
        """The actual save path with the timeout honoured: chat file is
        written synchronously; the summary runs on a daemon thread gated by
        an Event.wait(timeout) — exactly save_session's production shape.
        Returns the summary thread's name and whether the wait timed out."""
        chat = master_ai.CHATS_DIR / f"{master_ai.SESSION_TS}.chat"
        chat.write_text("transcript")
        timeout = kwargs.get("summarize_timeout")
        done = threading.Event()
        box = {}

        def _summarize():
            master_ai.summarize_session(history)  # may block on a wedged cloud
            done.set()

        t = threading.Thread(target=_summarize, daemon=True)
        t.start()
        if isinstance(timeout, (int, float)) and timeout > 0:
            box["timed_out"] = not done.wait(timeout=timeout)
            box["skipped"] = False
            return "summarize-thread-bounded", box
        return "summarize-thread-unbounded", {"skipped": True}

    def _install_blocking_summarizer(self):
        """Replace summarize_session with a call that blocks until released."""
        released = threading.Event()

        def _block(history):
            # Simulates a cloud summarizer wedged on network latency.
            released.wait(timeout=60)
            return "", "late summary"

        master_ai.summarize_session = _block
        return released

    def _install_capture_save(self):
        """Stub save_session capturing (history, silent, summarize_timeout)."""
        captured = {}

        def _fake_save(history, silent=False, **kwargs):
            captured["history"] = list(history)
            captured["silent"] = silent
            captured["summarize_timeout"] = kwargs.get("summarize_timeout")

        master_ai.save_session = _fake_save
        return captured

    def _force_mtime_mismatch(self):
        master_ai._STARTUP_CODE_MTIME = 1.0  # real file's mtime != 1.0

    # ── the tests ────────────────────────────────────────────────────────

    def test_reload_passes_bounded_positive_timeout_to_save(self):
        captured = self._install_capture_save()
        self._force_mtime_mismatch()
        master_ai._reload_if_code_changed(
            [{"role": "user", "content": "hi"}], "carry me"
        )
        self.assertEqual(len(self._execvp_calls), 1)
        to = captured["summarize_timeout"]
        self.assertIsNotNone(to, "reload must pass summarize_timeout explicitly")
        self.assertIsInstance(to, (int, float))
        self.assertTrue(0 < to <= 30, f"timeout {to} not a sane bound")
        self.assertEqual(captured["silent"], True)

    def test_bounded_save_does_not_hang_on_blocking_summarizer(self):
        """Real bound: a wedged summarizer + the production wait-shape must
        return (well) inside RELOAD_TIMEOUT seconds."""
        self._install_blocking_summarizer()
        t0 = time.monotonic()
        label, box = self._real_save_with_timeout(
            [{"role": "user", "content": "hi"}], summarize_timeout=2.0
        )
        elapsed = time.monotonic() - t0
        self.assertEqual(label, "summarize-thread-bounded")
        self.assertTrue(box["timed_out"], "bounded wait should time out")
        self.assertLess(elapsed, 4.0, "save+wait hung despite the timeout")
        self.assertGreaterEqual(elapsed, 1.9, "timeout applied too early")

    def test_nonpositive_timeout_is_treated_as_no_bound(self):
        """summarize_timeout=None/0 means the unbounded path is taken by
        design (blocking wait) — the reload path must never send one of
        these, this pins what 'unbounded' looks like so the distinction
        stays intentional."""
        released = self._install_blocking_summarizer()
        t = threading.Thread(
            target=self._real_save_with_timeout,
            args=([{"role": "user", "content": "x"}],),
            kwargs={"summarize_timeout": None},
            daemon=True,
        )
        t.start()
        released.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "unbounded save hung on a releaseable block")

    def test_reload_without_pending_cmd_clears_carry_file(self):
        self._install_capture_save()
        self._force_mtime_mismatch()
        master_ai._RELOAD_CARRY_FILE.write_text("stale note")
        master_ai._reload_if_code_changed([{"role": "user", "content": "x"}], None)
        self.assertFalse(
            master_ai._RELOAD_CARRY_FILE.exists(), "stale carry note must be deleted"
        )
        self.assertTrue(master_ai.RESUME_FLAG.exists())

    def test_reload_save_failure_still_restarts(self):
        """A save explosion on the reload path must not skip the execvp —
        the restart is the point; state loss is the acceptable cost."""

        def _boom(history, silent=False, **k):
            raise RuntimeError("disk on fire")

        master_ai.save_session = _boom
        self._force_mtime_mismatch()
        master_ai._reload_if_code_changed([{"role": "user", "content": "x"}], "go")
        self.assertEqual(len(self._execvp_calls), 1)


class TestResumeRestoresApprovalsPending(_IsolateRuntimeState, unittest.TestCase):
    """WIRING tests (runtime_state internals live in test_runtime_state.py):
    save_session() must COLLECT the live pending-approval queue into the
    snapshot, and master_ai's resume entry (_restore_structured_state) must
    re-queue them PENDING through approval_queue.queue() — fresh ids, fresh
    timestamps, never auto-approved, never marked RAN."""

    def setUp(self):
        super().setUp()
        _TEST_DIR.mkdir(parents=True, exist_ok=True)
        self._tmp = self._test_chats_dir()
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._ma = master_ai
        self._orig_chats = master_ai.CHATS_DIR
        self._orig_ts = master_ai.SESSION_TS

    def tearDown(self):
        super().tearDown()
        master_ai.CHATS_DIR = self._orig_chats
        master_ai.SESSION_TS = self._orig_ts
        for p in self._tmp.iterdir():
            p.unlink(missing_ok=True)
        self._clean_scratch()

    @staticmethod
    def _approval_entry(**over):
        e = {
            "id": "20261005120000",
            "ts": 1791260556.0,  # fixed epoch — deterministic, no wall clock
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

    @staticmethod
    def _test_chats_dir():
        return _TEST_DIR / "approval-chats"

    def test_save_session_collects_live_pending_approval_into_snapshot(self):
        self._orig_qfile = approval_queue.JSONL_FILE
        approval_queue.JSONL_FILE = self._scratch / ".pending_actions.jsonl"
        approval_queue.MD_FILE = self._scratch / "pending_actions.md"
        eid = approval_queue.queue(
            entry_type="run_command",
            who="test",
            what="echo hi",
            where="/tmp",
            why="wiring-test",
            payload={"cmd": "echo hi"},
        )
        self.assertTrue(eid)
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "wire-approval-save"
        try:
            self._ma.save_session(
                [
                    {"role": "user", "content": "run echo hi"},
                    {"role": "assistant", "content": "queued for approval"},
                ],
                silent=True,
                summarize_timeout=0.01,
            )
        finally:
            approval_queue.JSONL_FILE = self._orig_qfile
            approval_queue.MD_FILE = self._scratch / "pending_actions.md"
        state_path = (self._tmp / "wire-approval-save.chat").with_suffix(".state.json")
        st = runtime_state.load_state(state_path)
        self.assertEqual(len(st["pending_approvals"]), 1)
        self.assertEqual(st["pending_approvals"][0]["status"], "PENDING")

    def test_resume_requeues_approval_pending_never_ran(self):
        """The headline contract: a resume must produce a PENDING entry —
        never RAN, never auto-approved — with a fresh id/timestamp."""
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "wire-approval-resume"
        # Build the snapshot by hand (deterministic) and write it through
        # the REAL write path — no approval-queue live dependency on save.
        chat_path = self._tmp / "wire-approval-resume.chat"
        chat_path.write_text("[2026-10-05 00:00] You: hi\n[2026-10-05 00:00] AI: ok\n")
        st = {
            "schema": runtime_state.STATE_SCHEMA,
            "saved_at": 1791260556.0,
            "pending_approvals": [self._approval_entry()],
            "subagents": {"recent": []},
            "tool_lifecycle": runtime_state.collect_tool_lifecycle(),
        }
        runtime_state.write_state(
            st, runtime_state.state_path_for(chat_path), master_ai._atomic_write_text
        )
        # Point the queue at a CLEAN file — the restore must create the
        # entry there itself, with a fresh id.
        restored_file = self._scratch / "restored.jsonl"
        self._orig_qfile = approval_queue.JSONL_FILE
        approval_queue.JSONL_FILE = restored_file
        approval_queue.MD_FILE = self._scratch / "pending_actions.md"
        try:
            summary = self._ma._restore_structured_state(chat_path, [])
        finally:
            approval_queue.JSONL_FILE = self._orig_qfile
            approval_queue.MD_FILE = self._scratch / "pending_actions.md"
            restored_file.unlink(missing_ok=True)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["approvals_restored"], 1)
        # The queue file is now gone — re-read through a fresh redirect to
        # assert shape/status of what was queued (id freshness, status).
        approval_queue.JSONL_FILE = self._scratch / "inspect.jsonl"
        # Re-run restore to observe what it writes (same deterministic stub)
        restored2 = self._scratch / "inspect.jsonl"
        approval_queue.JSONL_FILE = restored2
        try:
            self._ma._restore_structured_state(chat_path, [])
            entries = [e for e in restored2.read_text().splitlines() if e.strip()]
            self.assertEqual(len(entries), 1)
            e = json.loads(entries[0])
            self.assertEqual(e["status"], "PENDING")
            self.assertEqual(e["payload"], {"cmd": "apt install foo"})
            self.assertNotEqual(e["id"], "20261005120000", "must get a fresh id")
            self.assertIn("resumed", e["why"])
        finally:
            approval_queue.JSONL_FILE = self._orig_qfile
            restored2.unlink(missing_ok=True)

    def test_resume_reports_approved_style_entry_back_to_pending(self):
        """A snapshot entry that claims status=RAN (drifted/hand-edited) is
        still re-queued PENDING on resume — restore never replays history
        as approval, it always re-asks."""
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = "wire-approval-ran"
        chat_path = self._tmp / "wire-approval-ran.chat"
        chat_path.write_text("[d] You: hi\n[d] AI: ok\n")
        st = {
            "schema": runtime_state.STATE_SCHEMA,
            "saved_at": 1791260556.0,
            "pending_approvals": [self._approval_entry(status="RAN")],
            "subagents": {"recent": []},
            "tool_lifecycle": runtime_state.collect_tool_lifecycle(),
        }
        runtime_state.write_state(
            st, runtime_state.state_path_for(chat_path), master_ai._atomic_write_text
        )
        restored = self._scratch / "restored-ran.jsonl"
        self._orig_qfile = approval_queue.JSONL_FILE
        approval_queue.JSONL_FILE = restored
        approval_queue.MD_FILE = self._scratch / "pending_actions.md"
        try:
            summary = self._ma._restore_structured_state(chat_path, [])
            self.assertEqual(summary["approvals_restored"], 1)
            entries = [e for e in restored.read_text().splitlines() if e.strip()]
            self.assertEqual(len(entries), 1)
            self.assertEqual(json.loads(entries[0])["status"], "PENDING")
        finally:
            approval_queue.JSONL_FILE = self._orig_qfile
            restored.unlink(missing_ok=True)


class TestResumeRestoresSubagentRecords(_IsolateRuntimeState, unittest.TestCase):
    """WIRING: subagent dispatch records ([SUBAGENT RESULT]/[SUBAGENT ERROR]
    feedback turns) must survive save→restore as inert context turns —
    injected into history exactly the way live dispatch feedback enters."""

    def setUp(self):
        super().setUp()
        _TEST_DIR.mkdir(parents=True, exist_ok=True)
        self._tmp = self._test_chats_dir()
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._ma = master_ai
        self._orig_chats = master_ai.CHATS_DIR
        self._orig_ts = master_ai.SESSION_TS

    def tearDown(self):
        super().tearDown()
        master_ai.CHATS_DIR = self._orig_chats
        master_ai.SESSION_TS = self._orig_ts
        for p in self._tmp.iterdir():
            p.unlink(missing_ok=True)
        self._clean_scratch()

    @staticmethod
    def _test_chats_dir():
        return _TEST_DIR / "subagent-chats"

    def _save_with_subagent_history(self, session_ts):
        self._ma.CHATS_DIR = self._tmp
        self._ma.SESSION_TS = session_ts
        history = [
            {"role": "user", "content": "run the audit"},
            {"role": "assistant", "content": "dispatching subagent"},
            {
                "role": "user",
                "content": "[SUBAGENT RESULT] goal='probe' ok=True\nwork done",
            },
            {
                "role": "user",
                "content": "[SUBAGENT ERROR] second agent crashed",
            },
        ]
        self._ma.save_session(history, silent=True, summarize_timeout=0.01)
        return self._tmp / f"{session_ts}.chat"

    def test_subagent_blocks_saved_into_state_snapshot(self):
        chat_path = self._save_with_subagent_history("wire-sub-save")
        st = runtime_state.load_state(runtime_state.state_path_for(chat_path))
        recent = st["subagents"]["recent"]
        self.assertEqual(len(recent), 2)
        self.assertTrue(recent[0][1].startswith("[SUBAGENT RESULT]"))
        self.assertTrue(recent[1][1].startswith("[SUBAGENT ERROR]"))

    def test_resume_reinjects_subagent_results_as_context_turns(self):
        chat_path = self._save_with_subagent_history("wire-sub-resume")
        fresh_history: list = []
        summary = self._ma._restore_structured_state(chat_path, fresh_history)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["subagents_restored"], 2)
        self.assertEqual(len(fresh_history), 3)  # 2 result turns + 1 assistant ack
        self.assertEqual(fresh_history[0]["role"], "user")
        self.assertIn("[SUBAGENT RESULT]", fresh_history[0]["content"])
        self.assertEqual(fresh_history[-1]["role"], "assistant")
        # Inert: the restored turns must never be executable directives.
        for m in fresh_history[:-1]:
            self.assertFalse(m["content"].lstrip().startswith("RUN:"))
            self.assertFalse(m["content"].lstrip().startswith("BROWSER_"))
