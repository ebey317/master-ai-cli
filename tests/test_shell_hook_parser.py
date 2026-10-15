"""Tests for shell hook response parser (pre_tool_call)."""

import pytest
from scripts.shell_hook_parser import parse_pre_tool_call_response


class TestApproveDirective:
    """The documented approve action escalates to the human approval gate."""

    def test_approve_with_message_and_rule_key_stripped(self):
        stdout = '{"action": "approve", "message": "  needs a human ", "rule_key": " terminal:rm "}'
        assert parse_pre_tool_call_response(stdout) == {
            "action": "approve",
            "message": "needs a human",
            "rule_key": "terminal:rm",
        }

    def test_approve_with_empty_optional_fields_dropped(self):
        stdout = '{"action": "approve", "message": "", "rule_key": 7}'
        assert parse_pre_tool_call_response(stdout) == {"action": "approve"}

    def test_approve_with_missing_optional_fields(self):
        stdout = '{"action": "approve"}'
        assert parse_pre_tool_call_response(stdout) == {"action": "approve"}


class TestDecisionApproveNotMapped:
    """Claude-Code's decision:approve means auto-ALLOW; never mapped to approval gate."""

    def test_decision_approve_returns_none(self):
        stdout = '{"decision": "approve", "reason": "ok"}'
        assert parse_pre_tool_call_response(stdout) is None


class TestBlockDirective:
    def test_hermes_block_with_message(self):
        stdout = '{"action": "block", "message": "nope"}'
        assert parse_pre_tool_call_response(stdout) == {"action": "block", "message": "nope"}

    def test_hermes_block_without_message(self):
        stdout = '{"action": "block"}'
        assert parse_pre_tool_call_response(stdout) == {"action": "block"}

    def test_claude_code_decision_block(self):
        stdout = '{"decision": "block", "reason": "not allowed"}'
        assert parse_pre_tool_call_response(stdout) == {"action": "block", "message": "not allowed"}

    def test_claude_code_decision_block_no_reason(self):
        stdout = '{"decision": "block"}'
        assert parse_pre_tool_call_response(stdout) == {"action": "block"}


class TestModifyDirective:
    def test_hermes_modify(self):
        stdout = '{"action": "modify", "args": {"new_string": "fixed"}}'
        assert parse_pre_tool_call_response(stdout) == {"action": "modify", "args": {"new_string": "fixed"}}

    def test_claude_code_decision_modify(self):
        stdout = '{"decision": "modify", "tool_input": {"new_string": "fixed"}}'
        assert parse_pre_tool_call_response(stdout) == {"action": "modify", "args": {"new_string": "fixed"}}


class TestEdgeCases:
    def test_empty_stdout_returns_none(self):
        assert parse_pre_tool_call_response("") is None
        assert parse_pre_tool_call_response("   ") is None

    def test_invalid_json_returns_none(self):
        assert parse_pre_tool_call_response("not json") is None
        assert parse_pre_tool_call_response("{") is None

    def test_non_dict_json_returns_none(self):
        assert parse_pre_tool_call_response('["array"]') is None
        assert parse_pre_tool_call_response('"string"') is None
        assert parse_pre_tool_call_response("123") is None

    def test_unknown_action_returns_none(self):
        stdout = '{"action": "unknown", "foo": "bar"}'
        assert parse_pre_tool_call_response(stdout) is None

    def test_action_approve_with_decision_block_prefers_block(self):
        # If both present, block takes precedence (defensive)
        stdout = '{"action": "approve", "decision": "block", "reason": "no"}'
        assert parse_pre_tool_call_response(stdout) == {"action": "block", "message": "no"}
