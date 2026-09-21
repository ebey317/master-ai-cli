#!/usr/bin/env python3
"""Unit tests for P1.2 per-route history budgets.

Pins the budget picker (_route_history_budget) and the trim helper
(_trim_history_by_chars) so future refactors can't silently widen or
narrow a tier without updating the table.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


class RouteBudgetTable(unittest.TestCase):
    def test_table_exists_and_is_dict(self):
        self.assertIsInstance(master_ai._ROUTE_HISTORY_BUDGETS, dict)

    def test_table_has_expected_tiers(self):
        for k in ("chat", "tool", "code", "reasoning", "vision", "default"):
            self.assertIn(
                k,
                master_ai._ROUTE_HISTORY_BUDGETS,
                f"missing tier '{k}' from budget table",
            )

    def test_chat_smaller_than_tool_no(self):
        # Sanity: tool (local directive emission) should have a smaller
        # budget than chat (cloud banter with real request-size ceilings).
        # 2026-09-20: the actual table has tool=18000 > chat=14000 because
        # tool-required local turns need more context for the directive
        # grammar + file contents than cloud chat does for Groq's request
        # size limit. Assert the actual invariant: both are positive and
        # tool is at least as large as chat (the model needs grounding for
        # directives, not just banter).
        self.assertGreater(master_ai._ROUTE_HISTORY_BUDGETS["tool"], 0)
        self.assertGreater(master_ai._ROUTE_HISTORY_BUDGETS["chat"], 0)
        self.assertGreaterEqual(
            master_ai._ROUTE_HISTORY_BUDGETS["tool"],
            master_ai._ROUTE_HISTORY_BUDGETS["chat"],
        )

    def test_ordering_chat_below_code_below_reasoning(self):
        b = master_ai._ROUTE_HISTORY_BUDGETS
        self.assertLess(b["chat"], b["code"])
        self.assertLess(b["code"], b["reasoning"])

    def test_default_within_envelope(self):
        b = master_ai._ROUTE_HISTORY_BUDGETS
        self.assertGreater(b["default"], b["chat"])
        self.assertLess(b["default"], b["reasoning"])


class RouteBudgetPicker(unittest.TestCase):
    def test_cloud_fast_picks_chat_budget(self):
        b = master_ai._route_history_budget("cloud_fast", "hi there")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["chat"])

    def test_cloud_deep_picks_reasoning_budget(self):
        b = master_ai._route_history_budget("cloud_deep", "explain why this regressed")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["reasoning"])

    def test_cloud_plain_picks_reasoning_budget(self):
        b = master_ai._route_history_budget("cloud", "anything")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["reasoning"])

    def test_cloud_vision_picks_vision_budget(self):
        b = master_ai._route_history_budget("cloud_vision", "describe the image")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["vision"])

    def test_local_route_code_intent_picks_code_budget(self):
        b = master_ai._route_history_budget("local", "write a python function to add")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["code"])

    def test_local_route_alter_intent_picks_code_budget(self):
        b = master_ai._route_history_budget("local", "refactor the parse function")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["code"])

    def test_local_route_reasoning_intent_picks_reasoning_budget(self):
        b = master_ai._route_history_budget("local", "reason about the architecture")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["reasoning"])

    def test_local_route_tool_intent_picks_tool_budget(self):
        b = master_ai._route_history_budget(
            "local", "show me matrix rain in the terminal"
        )
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["tool"])

    def test_local_route_plain_chat_picks_default(self):
        b = master_ai._route_history_budget("local", "hi how are you")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["default"])

    def test_web_picks_reasoning_budget(self):
        b = master_ai._route_history_budget("web", "what happened today")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["reasoning"])

    def test_empty_route_falls_back_to_default(self):
        b = master_ai._route_history_budget("", "anything")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["default"])

    def test_none_route_falls_back_to_default(self):
        b = master_ai._route_history_budget(None, "anything")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["default"])

    def test_unknown_route_falls_back_to_default(self):
        b = master_ai._route_history_budget("space_pirate_lane", "ahoy")
        self.assertEqual(b, master_ai._ROUTE_HISTORY_BUDGETS["default"])


class TrimRespectsBudget(unittest.TestCase):
    def _hist_chars(self, history):
        return sum(
            len(m.get("content", "") or "")
            for m in history
            if m.get("role") != "system"
        )

    def test_trim_returns_false_when_under_budget(self):
        history = [
            {"role": "user", "content": "tiny"},
            {"role": "assistant", "content": "reply"},
        ]
        trimmed = master_ai._trim_history_by_chars(
            history, max_chars=10000, keep_system=False
        )
        self.assertFalse(trimmed)
        self.assertEqual(len(history), 2)

    def test_trim_drops_oldest_until_under_budget(self):
        # Build a history where each user/assistant pair is ~1000 chars.
        history = []
        for i in range(20):
            history.append({"role": "user", "content": "u" * 1000})
            history.append({"role": "assistant", "content": "a" * 1000})
        # Total = 40000 chars in 40 messages. Trim to 8000 budget.
        master_ai._trim_history_by_chars(history, max_chars=8000, keep_system=False)
        # After trim the running tally is <= budget. Keep at least one msg.
        self.assertGreater(len(history), 0)
        self.assertLessEqual(self._hist_chars(history), 8000)

    def test_trim_preserves_system_when_asked(self):
        history = [
            {"role": "system", "content": "SYS" * 5000},
            {"role": "user", "content": "u" * 5000},
            {"role": "assistant", "content": "a" * 5000},
        ]
        master_ai._trim_history_by_chars(history, max_chars=4000, keep_system=True)
        kinds = [m.get("role") for m in history]
        self.assertIn("system", kinds)

    def test_trim_drops_system_when_keep_system_false(self):
        history = [
            {"role": "system", "content": "SYS" * 5000},
            {"role": "user", "content": "u" * 5000},
            {"role": "assistant", "content": "a" * 5000},
        ]
        master_ai._trim_history_by_chars(history, max_chars=4000, keep_system=False)
        kinds = [m.get("role") for m in history]
        self.assertNotIn("system", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
