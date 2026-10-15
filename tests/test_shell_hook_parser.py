"""Tests for the portable shell hook parser (Hermes-inspired approve escalation)."""

import pytest
from scripts.shell_hook_parser import parse_pre_tool_call


class TestParsePreToolCall:
    """Tests covering block, modify, and approve dialects."""

    # --- Block dialects ---
    def test_block_hermes_canonical(self):
        assert parse_pre_tool_call('{"action": "block", "message": "nope"}') == {
            "action": "block", "message": "nope"
        }

    def test_block_claude_code_style(self):
        assert parse_pre_tool_call('{"decision": "block", "reason": "denied"}') == {
            "action": "block", "message": "denied"
        }

    def test_block_empty_message_returns_block_without_message(self):
        assert parse_pre_tool_call('{"action": "block", "message": "  "}') == {"action": "block"}

    # --- Modify dialects ---
    def test_modify_hermes_canonical(self):
        assert parse_pre_tool_call('{"action": "modify", "args": {"x": 1}}') == {
            "action": "modify", "args": {"x": 1}
        }

    def test_modify_claude_code_style(self):
        assert parse_pre_tool_call('{"decision": "modify", "tool_input": {"x": 1}}') == {
            "action": "modify", "args": {"x": 1}
        }

    # --- Approve dialect (Hermes-only escalation to human gate) ---
    @pytest.mark.parametrize("stdout, expected", [
        ('{"action": "approve", "message": "  needs a human ", "rule_key": " terminal:rm "}',
         {"action": "approve", "message": "needs a human", "rule_key": "terminal:rm"}),
        ('{"action": "approve", "message": "", "rule_key": 7}', {"action": "approve"}),
        # Claude-Code's `decision: approve` means auto-ALLOW, not "ask a human": never mapped.
        ('{"decision": "approve", "reason": "ok"}', None),
        # If both action: approve and decision: block present, action wins (block takes precedence via dialect order)
        ('{"action": "approve", "decision": "block", "reason": "no"}', {"action": "block", "message": "no"}),
        # Approve with only rule_key
        ('{"action": "approve", "rule_key": "  scope:risky  "}', {"action": "approve", "rule_key": "scope:risky"}),
    ])
    def test_approve_is_parsed_like_the_plugin_directive(self, stdout, expected):
        """
        The documented `approve` action used to parse to None, so the tool ran with no
        approval prompt. It now yields the same shape Python plugins return.
        """
        assert parse_pre_tool_call(stdout) == expected

    # --- Edge cases ---
    def test_empty_stdout_returns_none(self):
        assert parse_pre_tool_call("") is None
        assert parse_pre_tool_call("   ") is None

    def test_invalid_json_returns_none(self):
        assert parse_pre_tool_call("not json") is None

    def test_non_dict_json_returns_none(self):
        assert parse_pre_tool_call('["not", "a", "dict"]') is None

    def test_unknown_action_returns_none(self):
        assert parse_pre_tool_call('{"action": "foobar"}') is None
