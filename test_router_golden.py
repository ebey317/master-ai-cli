#!/usr/bin/env python3
"""Golden routing tests for Master AI.

Tests run against the router.py public surface, which delegates to
master_ai.orchestrate(). They pin the decision (route, model selection
heuristics, synth-reply presence) for representative inputs across:
chat, code, filesystem/system-query, current-events, vision (explicit
AND negated), terminal-visual, reasoning, weather, recall, and messy
voice-to-text input.

Tests do NOT call any LLM or run shell commands — orchestrate() is pure
decision logic. To stay deterministic the harness:

  * sets MODE='plan' so the harvest cache lookup is bypassed,
  * monkeypatches load_keys() to {} so cloud lanes don't auto-engage,
  * monkeypatches _read_run_mode() to 'apocalypse' (local-first default).

Run: python3 ~/scripts/test_router_golden.py
Exit: 0 = all pass, non-zero = at least one route mismatch.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402
import router  # noqa: E402


class RouterGoldenBase(unittest.TestCase):
    def setUp(self):
        self._orig_load_keys = master_ai.load_keys
        self._orig_read_run_mode = master_ai._read_run_mode
        self._orig_mode = getattr(master_ai, "MODE", "plan")
        self._orig_pinned = getattr(master_ai, "PINNED_MODEL", "")

        master_ai.load_keys = lambda: {}
        master_ai._read_run_mode = lambda: "apocalypse"
        master_ai.MODE = "plan"
        master_ai.PINNED_MODEL = ""

    def tearDown(self):
        master_ai.load_keys = self._orig_load_keys
        master_ai._read_run_mode = self._orig_read_run_mode
        master_ai.MODE = self._orig_mode
        master_ai.PINNED_MODEL = self._orig_pinned

    def decide(self, text, history=None, image_path=None) -> dict:
        return router.route(history or [], text, image_path=image_path)


class ChatRouting(RouterGoldenBase):
    def test_greeting_routes_local_no_cloud_keys(self):
        d = self.decide("hi")
        self.assertEqual(d["route"], "local")
        self.assertIn("model", d)

    def test_thanks_routes_local(self):
        # "thanks" is a pure acknowledgment (commit 555cc09) — short-
        # circuited to a deterministic one-line reply with no model call.
        d = self.decide("thanks")
        self.assertEqual(d["route"], "acknowledgment")
        self.assertIn("response", d)

    def test_plain_question_routes_local(self):
        d = self.decide("what is the capital of France")
        self.assertEqual(d["route"], "local")


class CodeRouting(RouterGoldenBase):
    def test_write_function_routes_to_coder(self):
        d = self.decide("write a python function to add two numbers")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["model"], master_ai.MODELS["coder"])

    def test_refactor_request_stays_local(self):
        # 'refactor' is in ALTER_WORDS; cloud lanes can't touch disk so this
        # must route locally. Master-ai model is the qwen2.5:7b + Sensei
        # SYSTEM lane (the brain), which is correct for editing intents.
        d = self.decide("refactor the parse function in master_ai.py")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["model"], master_ai.MODELS["master"])


class SystemQueryRouting(RouterGoldenBase):
    # The original "system_query" route (_system_query_short_circuit) was
    # deleted in commit aad2762 and reintroduced under a new name,
    # "deterministic_intent" (_deterministic_intent_to_directive, commit
    # 953f31a) — same behavior (synthesizes a RUN:/READ: directive, no
    # model call), new route string.

    def test_where_is_file_short_circuits(self):
        d = self.decide("where is biovega_field_manual.md on my computer")
        self.assertEqual(d["route"], "deterministic_intent")
        self.assertIn("synth_reply", d)
        self.assertTrue(d["synth_reply"])
        self.assertTrue(
            "RUN:" in d["synth_reply"] or "READ:" in d["synth_reply"],
            f"synth_reply missing RUN:/READ: directive: {d['synth_reply']!r}",
        )

    def test_port_check_short_circuits(self):
        d = self.decide("what's on port 8080")
        self.assertEqual(d["route"], "deterministic_intent")
        self.assertIn("synth_reply", d)

    def test_is_service_running_short_circuits(self):
        d = self.decide("is ollama running")
        self.assertEqual(d["route"], "deterministic_intent")

    def test_messy_voice_where_is(self):
        # Phone voice-to-text often drops punctuation and concatenates words.
        d = self.decide("where is the libreoffice templates folder on disk")
        self.assertEqual(d["route"], "deterministic_intent")


class VisionRouting(RouterGoldenBase):
    def test_image_path_routes_to_vision(self):
        d = self.decide("describe /tmp/photo.png for me")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["model"], master_ai.MODELS["vision"])

    def test_explicit_image_attached_routes_vision(self):
        d = self.decide("what is this", image_path="/tmp/foo.jpg")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["model"], master_ai.MODELS["vision"])

    def test_describe_the_image_phrase(self):
        d = self.decide("please describe the image")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["model"], master_ai.MODELS["vision"])

    # ── Vision negation guard (P0.3 fix) ─────────────────────────
    # These all use vision-trigger words but with explicit negation.
    # They must NOT route to vision.

    def test_negated_dont_describe_image(self):
        d = self.decide("don't describe the image, just summarize it as text")
        self.assertNotEqual(d.get("model"), master_ai.MODELS["vision"],
            f"negated 'don't describe' routed to vision: {d}")

    def test_negated_no_picture_attached(self):
        d = self.decide("I don't have a picture, just text")
        self.assertNotEqual(d.get("model"), master_ai.MODELS["vision"],
            f"'I don't have a picture' routed to vision: {d}")

    def test_negated_without_showing_image(self):
        d = self.decide("without showing me the image, tell me what it should contain")
        self.assertNotEqual(d.get("model"), master_ai.MODELS["vision"],
            f"'without showing image' routed to vision: {d}")

    def test_negated_no_screenshot_involved(self):
        d = self.decide("there is no screenshot involved in this question")
        self.assertNotEqual(d.get("model"), master_ai.MODELS["vision"],
            f"'no screenshot involved' routed to vision: {d}")


class TerminalVisualRouting(RouterGoldenBase):
    def test_matrix_rain_is_tool_required(self):
        d = self.decide("show me matrix rain in the terminal")
        self.assertEqual(d["route"], "local")
        # Tool-required visual stays on master-ai
        self.assertEqual(d["model"], master_ai.MODELS["master"])


class ReasoningRouting(RouterGoldenBase):
    def test_reasoning_words_local_when_no_cloud(self):
        d = self.decide("reason about why this approach is slower than the alternative")
        # No cloud keys → local route (apocalypse fallback)
        self.assertEqual(d["route"], "local")


class WeatherRouting(RouterGoldenBase):
    def test_whats_the_weather_short_circuits(self):
        # The weather short-circuit (fed wttr.in) was deleted for good in
        # commit aad2762, with no replacement — "what's the weather" now
        # falls through to a normal local chat turn, same as any other
        # question the deterministic-intent/ack layers don't recognize.
        d = self.decide("what's the weather")
        self.assertEqual(d["route"], "local")


class CurrentEventsRouting(RouterGoldenBase):
    def test_time_sensitive_warn_in_apocalypse(self):
        d = self.decide("what happened at wrestlemania last night")
        self.assertEqual(d["route"], "time_sensitive_warn")
        self.assertIn("original_query", d)


class ExplicitPrefixRouting(RouterGoldenBase):
    def test_local_prefix_forces_local(self):
        d = self.decide("local: explain something")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["model"], master_ai.MODELS["master"])
        self.assertIn("stripped_text", d)
        self.assertEqual(d["stripped_text"], "explain something")

    def test_private_prefix_forces_local(self):
        d = self.decide("private: very confidential thing")
        self.assertEqual(d["route"], "local")
        self.assertEqual(d["model"], master_ai.MODELS["master"])


class RecallRouting(RouterGoldenBase):
    def test_remember_phrase_triggers_recall(self):
        # Recall requires a memory file present; the recall payload is empty
        # if memory is empty, in which case orchestrate falls through. We
        # only assert that IF recall fires, the route shape is sane.
        d = self.decide("remember what we talked about yesterday")
        # recall_memory (if MEMORY file exists with content), local (if
        # memory empty and nothing else claims it), or time_sensitive_warn
        # — "yesterday" is a strong time-sensitive marker (see
        # _looks_time_sensitive) and this base's apocalypse run_mode routes
        # unclaimed time-sensitive queries there before falling to local.
        # All three are valid in test environments.
        self.assertIn(d["route"], ("recall_memory", "local", "time_sensitive_warn"))


class RouteDecisionShape(RouterGoldenBase):
    def test_decision_normalization_roundtrip(self):
        d = self.decide("hi")
        decision = router.RouteDecision.from_dict(d)
        self.assertEqual(decision.route, d["route"])
        # to_dict round-trips the known keys (extras stay opaque)
        back = decision.to_dict()
        self.assertEqual(back["route"], d["route"])
        self.assertEqual(back["reason"], d["reason"])

    def test_decision_rejects_missing_route(self):
        with self.assertRaises(ValueError):
            router.RouteDecision.from_dict({"reason": "no route key"})

    def test_decision_rejects_non_dict(self):
        with self.assertRaises(TypeError):
            router.RouteDecision.from_dict("not a dict")


class HarvestRecordedOnDeterministicShortCircuit(RouterGoldenBase):
    """Pins the system-query harvest recording fix (P0.3).

    When the deterministic system_query / weather short-circuit fires, the
    dispatcher must call harvest.record() so identical queries serve from
    cache next time. The pre-fix code only recorded LLM-call paths (local,
    local_stream, cloud) — system_query/weather bypassed the model and
    therefore bypassed the cache too. Source inspection is the right test
    here because the dispatch is inline in handle() and a future refactor
    that drops the record() call would silently regress the cache.
    """

    def test_handle_records_harvest_for_system_query_route(self):
        import inspect
        src = inspect.getsource(master_ai.handle)
        # The "system_query" route name itself was retired in commit
        # aad2762 and its behavior reintroduced as "deterministic_intent"
        # (commit 953f31a). We look for the harvest.record call appearing
        # in the same source after the route check; cheapest reliable
        # proxy is "deterministic_intent" string + "harvest.record" string
        # both present.
        self.assertIn('deterministic_intent', src,
            "handle() lost the deterministic_intent route branch entirely")
        self.assertIn('harvest.record', src,
            "handle() has no harvest.record call — deterministic_intent "
            "route won't populate the cache. P0.3 regression.")
        # Pin the specific deterministic-route harvest call so a refactor
        # can't remove just THIS one without breaking the test.
        self.assertIn('task_type="deterministic"', src,
            "deterministic_intent dispatch missing "
            "harvest.record(..., task_type='deterministic') — P0.3 fix lost")


class RuntermBlockedFeedbackPinned(RouterGoldenBase):
    """Regression guard for RUNTERM blocked-action history feedback.

    Codex's 2026-05-05 wiring pass landed _append_tool_blocked_feedback for
    the RUNTERM dispatch loop. This test pins the wiring so a future refactor
    can't silently delete it.
    """

    def test_runterm_loop_consults_last_blocked_action(self):
        import inspect
        src = inspect.getsource(master_ai.process_reply)
        # Both RUN and RUNTERM branches must consume _LAST_BLOCKED_ACTION
        # through _append_tool_blocked_feedback. The helper itself is the
        # consumer; the loop calls it.
        self.assertIn("_append_tool_blocked_feedback", src)
        # Specifically the RUNTERM kind must be passed
        self.assertIn('_append_tool_blocked_feedback("RUNTERM"', src)
        self.assertIn('_append_tool_blocked_feedback("RUN"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
