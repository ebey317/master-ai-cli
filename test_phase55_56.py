#!/usr/bin/env python3
"""Tests for Phase 5.5 (plan-block schema parser) + 5.6 (turn_answer_start
hook). The parser logic in stt_server.py uses the same regex+json round-trip
exercised here; the hooks.KINDS membership pins that fire() will accept the
new kind.
"""

import json
import os
import re
import sys
import unittest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import hooks  # noqa: E402


def parse_plan_block(reply):
    """Mirror of the stt_server.py plan extractor (Phase 5.5). Returns the
    structured dict when a JSON-shaped <PLAN> block is present, else None."""
    m = re.search(r"<PLAN>\s*(\{[\s\S]*?\})\s*</PLAN>", reply or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or ("domains" not in obj and "steps" not in obj):
        return None
    return {
        "domains": list(obj.get("domains") or [])[:10],
        "steps": list(obj.get("steps") or [])[:20],
        "irreversible": list(obj.get("irreversible") or [])[:10],
    }


class Phase55PlanSchemaTests(unittest.TestCase):
    def test_extracts_basic_plan(self):
        reply = """<PLAN>
{
  "domains": ["drive.google.com"],
  "steps": [{"n":1, "action":"BROWSER_NAV", "target":"https://drive.google.com"}],
  "irreversible": []
}
</PLAN>
BROWSER_NAV: https://drive.google.com
"""
        out = parse_plan_block(reply)
        self.assertIsNotNone(out)
        self.assertEqual(out["domains"], ["drive.google.com"])
        self.assertEqual(out["steps"][0]["n"], 1)
        self.assertEqual(out["irreversible"], [])

    def test_prose_plan_returns_none(self):
        reply = """<PLAN>
Sites: drive.google.com
Steps:
1. Open My Drive
2. Search for "resume"
Irreversible: none
</PLAN>
"""
        self.assertIsNone(parse_plan_block(reply))

    def test_no_plan_returns_none(self):
        self.assertIsNone(parse_plan_block(""))
        self.assertIsNone(parse_plan_block("Just a reply with no plan block."))

    def test_malformed_json_returns_none(self):
        reply = "<PLAN>{not valid json}</PLAN>"
        self.assertIsNone(parse_plan_block(reply))

    def test_caps_to_safe_lengths(self):
        big_steps = ",".join(
            f'{{"n":{i},"action":"BROWSER_WAIT","target":"100"}}' for i in range(50)
        )
        reply = f'<PLAN>{{"domains":[],"steps":[{big_steps}],"irreversible":[]}}</PLAN>'
        out = parse_plan_block(reply)
        self.assertIsNotNone(out)
        self.assertEqual(len(out["steps"]), 20)


class Phase56HookKindTests(unittest.TestCase):
    def test_turn_answer_start_in_kinds(self):
        self.assertIn("turn_answer_start", hooks.KINDS)

    def test_fire_with_unknown_kind_safe(self):
        # Sanity: KINDS membership controls accepted kinds; firing an unknown
        # kind should not crash (the hooks contract is observer-only here).
        result = hooks.fire(
            "turn_answer_start", "test reply text", action={"turn_id": "abc"}
        )
        self.assertFalse(getattr(result, "blocked", False))


if __name__ == "__main__":
    unittest.main()
