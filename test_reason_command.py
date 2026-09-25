#!/usr/bin/env python3
"""Unit tests for the P1.3 reason-command parser.

Pins _parse_reason_command(user_text) → (depth, query) or None across all
four supported forms (legacy 'reason:' prefix, 'reason <depth>: <q>',
'reason <depth> <q>', bare 'reason <q>'). Also exercises handle_tight_
reasoning's depth validation by inspecting _REASON_DEPTHS.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


class ReasonDepthsConstant(unittest.TestCase):
    def test_four_depths(self):
        self.assertEqual(
            master_ai._REASON_DEPTHS,
            frozenset({"fast", "standard", "deep", "max"}),
        )


class ParseReasonLegacyPrefix(unittest.TestCase):
    def test_legacy_prefix_default_deep(self):
        r = master_ai._parse_reason_command("reason: why is this slow")
        self.assertEqual(r, ("deep", "why is this slow"))

    def test_legacy_prefix_with_whitespace(self):
        r = master_ai._parse_reason_command("   reason:    why is this slow   ")
        self.assertEqual(r, ("deep", "why is this slow"))

    def test_legacy_prefix_empty_query(self):
        # 'reason:' with nothing after still parses; the handler prints
        # usage. Parser returns ("deep", "").
        r = master_ai._parse_reason_command("reason:")
        self.assertEqual(r, ("deep", ""))


class ParseReasonDepthColon(unittest.TestCase):
    def test_fast(self):
        r = master_ai._parse_reason_command("reason fast: quick thought")
        self.assertEqual(r, ("fast", "quick thought"))

    def test_standard(self):
        r = master_ai._parse_reason_command("reason standard: middle effort")
        self.assertEqual(r, ("standard", "middle effort"))

    def test_deep(self):
        r = master_ai._parse_reason_command("reason deep: longer think")
        self.assertEqual(r, ("deep", "longer think"))

    def test_max(self):
        r = master_ai._parse_reason_command("reason max: hardest case")
        self.assertEqual(r, ("max", "hardest case"))

    def test_case_insensitive(self):
        r = master_ai._parse_reason_command("Reason DEEP: question")
        self.assertEqual(r, ("deep", "question"))


class ParseReasonDepthSpace(unittest.TestCase):
    def test_fast_no_colon(self):
        r = master_ai._parse_reason_command("reason fast quick thought")
        self.assertEqual(r, ("fast", "quick thought"))

    def test_max_no_colon(self):
        r = master_ai._parse_reason_command("reason max hardest case")
        self.assertEqual(r, ("max", "hardest case"))

    def test_depth_with_no_query_returns_none(self):
        # 'reason fast' with nothing after has no query — parser returns
        # None and the dispatcher falls through to normal handling.
        self.assertIsNone(master_ai._parse_reason_command("reason fast"))


class ParseReasonBare(unittest.TestCase):
    def test_bare_reason_word_with_query_defaults_deep(self):
        r = master_ai._parse_reason_command("reason why is this slow")
        self.assertEqual(r, ("deep", "why is this slow"))

    def test_bare_reason_alone_returns_none(self):
        self.assertIsNone(master_ai._parse_reason_command("reason"))

    def test_bare_reason_with_whitespace_alone_returns_none(self):
        self.assertIsNone(master_ai._parse_reason_command("  reason  "))


class ParseReasonNonMatches(unittest.TestCase):
    def test_unrelated_text(self):
        self.assertIsNone(master_ai._parse_reason_command("hi how are you"))

    def test_empty(self):
        self.assertIsNone(master_ai._parse_reason_command(""))
        self.assertIsNone(master_ai._parse_reason_command("   "))

    def test_none_input(self):
        self.assertIsNone(master_ai._parse_reason_command(None))

    def test_reasoning_word_not_a_command(self):
        # 'reasoning is a thing' starts with 'reasoning', not 'reason '.
        # Form-3 regex `^reason\s+...` requires whitespace after 'reason',
        # but 'reasoning' has 'i' after 'reason' — no match.
        self.assertIsNone(master_ai._parse_reason_command("reasoning is a thing"))

    def test_unknown_depth_falls_through_to_bare(self):
        # 'reason aggressive': aggressive isn't a recognized depth. Form-2
        # regex doesn't match (alternation is fast|standard|deep|max).
        # Form-3 bare regex DOES match — and 'aggressive' is the first
        # word of the query, treated as part of the question.
        r = master_ai._parse_reason_command("reason aggressive question")
        self.assertEqual(r, ("deep", "aggressive question"))


class HandleTightReasoningDepthGuard(unittest.TestCase):
    """Pin that handle_tight_reasoning accepts the new depth kwarg.

    Doesn't actually run the loop (would call into Ollama). Just verifies
    the signature has 'depth' and the default preserves legacy behavior.
    """

    def test_signature_has_depth_with_deep_default(self):
        import inspect

        sig = inspect.signature(master_ai.handle_tight_reasoning)
        self.assertIn("depth", sig.parameters)
        self.assertEqual(sig.parameters["depth"].default, "deep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
