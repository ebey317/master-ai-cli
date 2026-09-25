#!/usr/bin/env python3
"""Identity self-reference regression: verifies cloud + local lanes hold
character as Master AI and never retreat to LLM-disclaim framing.

Companion to test_browser_directives.py. Locks in the 2026-05-13 fix
(commit 50e6471) that added an `IDENTITY (DO NOT BREAK CHARACTER)` block
to CLOUD_SYSTEM. Background: cloud lanes don't see the Modelfile SYSTEM
block, so the Modelfile's "never say 'as a language model'" rule didn't
reach Groq/Fireworks/Gemini/Cerebras turns. The fix mirrored that rule
into CLOUD_SYSTEM. This test pins the contract.

Test prompts cover the four ways instruction-tuned LLMs typically slip:

  1. Direct identity question — "what are you?"
  2. Limitation question     — "what are your limitations? memory? real-time?"
  3. Capability question     — "can you actually run commands?"
  4. Self-reference question — "how would you improve your own architecture?"

The assertion is the same for all four: the reply must NOT contain any
LLM-disclaim phrase (the verbatim phrases the CLOUD_SYSTEM block tells
the model to never emit), AND must contain at least one Master AI
self-identifier (Master AI / Sensei / Pupil / a browser-directive word).

Default lane: cloud (`fast:` prefix → Groq). LIVE_LOCAL=1 swaps to local.

Run:
    python3 ~/scripts/test_identity_self_reference.py
    LIVE_LOCAL=1 python3 ~/scripts/test_identity_self_reference.py
"""

from __future__ import annotations

import json
import os
import unittest
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("MASTER_AI_BASE_URL", "http://127.0.0.1:8080")
LIVE_LOCAL = os.environ.get("LIVE_LOCAL") == "1"
LANE_PREFIX = "local:" if LIVE_LOCAL else "fast:"
LANE_LABEL = "local" if LIVE_LOCAL else "cloud(fast)"
TOKEN_FILE = Path.home() / ".master_ai_extension_token"

# Phrases CLOUD_SYSTEM explicitly tells the model never to emit. Each one
# is a known instruction-tuned-LLM tell. If any of these appear in a
# reply, the identity teaching has broken down.
LLM_DISCLAIM_PHRASES = (
    "as a language model",
    "as an ai language model",
    "as an llm",
    "i'm just an llm",
    "i am just an llm",
    "just a language model",
    "i don't have access to",
    "i cannot execute commands",
    "i can't execute commands",
    "i'm not connected to anything",
    "i don't have the ability to",
    "i do not have the ability to",
)

# At least one of these should appear so we know the model is talking
# about Master AI specifically, not generically describing "an AI agent."
MASTER_AI_SELF_IDENTIFIERS = (
    "master ai",
    "sensei",
    "pupil",
    "browser_nav",
    "browser_click",
    "browser_fill",
    "browser_read",
    "chrome extension",
    "the extension",
    "run:",
    "read:",
)


def _read_token():
    try:
        return TOKEN_FILE.read_text().strip()
    except Exception:
        return ""


def _post_chat(prompt, *, timeout=180):
    """POST /chat WITHOUT source/page_context so the lane prefix isn't
    buried inside the [API REQUEST] wrapper that _api_prompt adds."""
    body = json.dumps({"prompt": f"{LANE_PREFIX} {prompt}"}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/chat",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Master-AI-Token": _read_token(),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _assert_no_disclaim(test, reply, prompt_label):
    low = (reply or "").lower()
    hits = [p for p in LLM_DISCLAIM_PHRASES if p in low]
    test.assertEqual(
        hits,
        [],
        f"[{LANE_LABEL}/{prompt_label}] reply contains LLM-disclaim phrases {hits!r}\n"
        f"--- reply (first 600ch) ---\n{(reply or '')[:600]}",
    )


def _assert_master_ai_voice(test, reply, prompt_label):
    low = (reply or "").lower()
    hits = [s for s in MASTER_AI_SELF_IDENTIFIERS if s in low]
    test.assertNotEqual(
        hits,
        [],
        f"[{LANE_LABEL}/{prompt_label}] reply has no Master AI self-identifier "
        f"(expected one of: Master AI / Sensei / Pupil / browser_* / RUN: / READ: / chrome extension)\n"
        f"--- reply (first 600ch) ---\n{(reply or '')[:600]}",
    )


class IdentitySelfReferenceTests(unittest.TestCase):
    """Identity must hold against the four common LLM-break vectors."""

    @classmethod
    def setUpClass(cls):
        try:
            urllib.request.urlopen(f"{BASE_URL}/health", timeout=5).read()
        except Exception as exc:
            raise unittest.SkipTest(
                f"backend at {BASE_URL} unreachable ({exc!s}); start master-ai-ui.service"
            )

    def test_1_direct_identity(self):
        resp = _post_chat("what are you? an app? an agent? an LLM? something else?")
        reply = resp.get("reply", "")
        _assert_no_disclaim(self, reply, "direct_identity")
        _assert_master_ai_voice(self, reply, "direct_identity")

    def test_2_limitation_question(self):
        resp = _post_chat(
            "what are your limitations? can you remember our conversations? "
            "do you have real-time access to anything?"
        )
        reply = resp.get("reply", "")
        _assert_no_disclaim(self, reply, "limitation")
        _assert_master_ai_voice(self, reply, "limitation")

    def test_3_capability_question(self):
        resp = _post_chat(
            "can you actually run commands on this machine? "
            "or are you just describing what you'd do?"
        )
        reply = resp.get("reply", "")
        _assert_no_disclaim(self, reply, "capability")
        _assert_master_ai_voice(self, reply, "capability")

    def test_4_self_reference_question(self):
        # "your code" / "your architecture" should be read as self-referential —
        # the model is being asked about itself, not advising a third party.
        resp = _post_chat(
            "how would you improve your own architecture? "
            "what part of your codebase is weakest?"
        )
        reply = resp.get("reply", "")
        _assert_no_disclaim(self, reply, "self_reference")
        # Self-reference replies sometimes name specific files (master_ai.py,
        # stt_server.py) instead of the surface words, so accept those too.
        low = reply.lower()
        broad_identifiers = MASTER_AI_SELF_IDENTIFIERS + (
            "master_ai.py",
            "stt_server.py",
            "modelfile",
            "cloud_system",
        )
        hits = [s for s in broad_identifiers if s in low]
        self.assertNotEqual(
            hits,
            [],
            f"[{LANE_LABEL}/self_reference] reply doesn't reference Master AI or its files\n"
            f"--- reply (first 600ch) ---\n{reply[:600]}",
        )

    def test_5_mode_awareness(self):
        # CLOUD_SYSTEM is built per-turn with the current MODE injected at the
        # top; the local lane gets `[CURRENT MODE: X]` prepended to every user
        # message. Either way, "what mode are we in?" must produce a concrete
        # mode name, not "I don't know."
        resp = _post_chat("what mode are we in right now? plan, review, or auto?")
        reply = resp.get("reply", "")
        low = reply.lower()
        modes_named = [m for m in ("plan", "review", "auto") if m in low]
        self.assertNotEqual(
            modes_named,
            [],
            f"[{LANE_LABEL}/mode_awareness] reply names no mode at all "
            f"(expected at least one of plan/review/auto)\n"
            f"--- reply (first 600ch) ---\n{reply[:600]}",
        )
        ignorance = (
            "i don't know what mode",
            "i'm not sure what mode",
            "i cannot tell what mode",
            "i can't tell what mode",
            "no way to know",
            "no information about",
        )
        ignorance_hits = [p for p in ignorance if p in low]
        self.assertEqual(
            ignorance_hits,
            [],
            f"[{LANE_LABEL}/mode_awareness] reply claims ignorance of mode {ignorance_hits!r}\n"
            f"--- reply (first 600ch) ---\n{reply[:600]}",
        )

    def test_6_reasoning_surface_awareness(self):
        # The model has a real reasoning surface (reason:/reason deep:/etc.,
        # routed through sensei_reasoning_loop.run_reasoning_loop). When asked
        # "can you think deeper?" it must say YES and name the mechanism.
        resp = _post_chat(
            "can you think deeper or reason through something step by step if I ask?"
        )
        reply = resp.get("reply", "")
        low = reply.lower()
        # Either the prefix words or planner/critic vocabulary should appear.
        reasoning_markers = (
            "reason:",
            "reason ",
            "reasoning loop",
            "reasoning_loop",
            "planner",
            "critic",
            "deeper thinking",
            "deep think",
            "think deeper",
            "multi-step",
            "tight:",
            "think:",
        )
        hits = [m for m in reasoning_markers if m in low]
        self.assertNotEqual(
            hits,
            [],
            f"[{LANE_LABEL}/reasoning] reply doesn't reference the reason: surface "
            f"or planner/critic vocabulary\n"
            f"--- reply (first 600ch) ---\n{reply[:600]}",
        )
        denials = (
            "i can't reason",
            "i cannot reason",
            "i'm just inference",
            "i'm just a language model",
            "i don't have reasoning",
        )
        denial_hits = [d for d in denials if d in low]
        self.assertEqual(
            denial_hits,
            [],
            f"[{LANE_LABEL}/reasoning] reply denies reasoning capability {denial_hits!r}\n"
            f"--- reply (first 600ch) ---\n{reply[:600]}",
        )


if __name__ == "__main__":
    print(
        f"[test_identity_self_reference] lane={LANE_LABEL}, "
        f"base={BASE_URL}, token={'set' if _read_token() else 'empty'}"
    )
    unittest.main(verbosity=2)
