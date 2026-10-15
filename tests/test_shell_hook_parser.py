"""Tests for shell_hook_parser.parse_pre_tool_call."""

import pytest
from scripts.shell_hook_parser import parse_pre_tool_call, format_approve_directive


class TestParsePreToolCall:
    # approve escalation (Hermes-canonical)
    def test_approve_with_message_and_rule_key(self):
        out = parse_pre_tool_call(
            '{"action": "approve", "message": "  needs a human ", "rule_key": " terminal:rm "}'
        )
        assert out == {
            "action": "approve",
            "message": "needs a human",
            "rule_key": "terminal:rm",
        }

    def test_approve_empty_optionals_omitted(self):
        out = parse_pre_tool_call('{"action": "approve", "message": "", "rule_key": 7}')
        assert out == {"action": "approve"}

    def test_approve_only_message(self):
        out = parse_pre_tool_call('{"action": "approve", "message": "risky op"}')
        assert out == {"action": "approve", "message": "risky op"}

    def test_approve_only_rule_key(self):
        out = parse_pre_tool_call('{"action": "approve", "rule_key": "scope:delete"}')
        assert out == {"action": "approve", "rule_key": "scope:delete"}

    # block (Hermes-canonical)
    def test_block_with_message(self):
        out = parse_pre_tool_call('{"action": "block", "message": "nope"}')
        assert out == {"action": "block", "message": "nope"}

    def test_block_empty_message_omitted(self):
        out = parse_pre_tool_call('{"action": "block", "message": "  "}')
        assert out == {"action": "block"}

    # modify (Hermes-canonical)
    def test_modify_with_args(self):
        out = parse_pre_tool_call('{"action": "modify", "args": {"cmd": "echo hi"}}')
        assert out == {"action": "modify", "args": {"cmd": "echo hi"}}

    def test_modify_non_dict_args_ignored(self):
        out = parse_pre_tool_call('{"action": "modify", "args": "not a dict"}')
        assert out is None

    # Claude-Code compat: decision:block → block
    def test_decision_block_with_reason(self):
        out = parse_pre_tool_call('{"decision": "block", "reason": "denied"}')
        assert out == {"action": "block", "message": "denied"}

    def test_decision_block_empty_reason_omitted(self):
        out = parse_pre_tool_call('{"decision": "block", "reason": ""}')
        assert out == {"action": "block"}

    # Claude-Code compat: decision:modify → modify
    def test_decision_modify_with_tool_input(self):
        out = parse_pre_tool_call('{"decision": "modify", "tool_input": {"x": 1}}')
        assert out == {"action": "modify", "args": {"x": 1}}

    # decision:approve is auto-allow → NOT mapped
    def test_decision_approve_not_mapped(self):
        out = parse_pre_tool_call('{"decision": "approve", "reason": "ok"}')
        assert out is None

    # Mixed/conflicting keys: action takes precedence over decision
    def test_action_approve_overrides_decision_block(self):
        out = parse_pre_tool_call(
            '{"action": "approve", "decision": "block", "reason": "no"}'
        )
        assert out == {"action": "approve"}

    def test_action_block_overrides_decision_approve(self):
        out = parse_pre_tool_call(
            '{"action": "block", "decision": "approve", "message": "denied"}'
        )
        assert out == {"action": "block", "message": "denied"}

    # Invalid/empty input
    def test_empty_stdout_returns_none(self):
        assert parse_pre_tool_call("") is None
        assert parse_pre_tool_call("   ") is None

    def test_invalid_json_returns_none(self):
        assert parse_pre_tool_call("not json") is None
        assert parse_pre_tool_call("{") is None

    def test_non_dict_json_returns_none(self):
        assert parse_pre_tool_call('["array"]') is None
        assert parse_pre_tool_call('"string"') is None
        assert parse_pre_tool_call("123") is None

    def test_unrecognized_keys_returns_none(self):
        assert parse_pre_tool_call('{"foo": "bar"}') is None
        assert parse_pre_tool_call('{"action": "unknown"}') is None


class TestFormatApproveDirective:
    def test_basic(self):
        line = format_approve_directive()
        assert json.loads(line) == {"action": "approve"}

    def test_with_message(self):
        line = format_approve_directive(message="  needs review  ")
        assert json.loads(line) == {"action": "approve", "message": "needs review"}

    def test_with_rule_key(self):
        line = format_approve_directive(rule_key="  terminal:rm  ")
        assert json.loads(line) == {"action": "approve", "rule_key": "terminal:rm"}

    def test_with_both(self):
        line = format_approve_directive(message="risky", rule_key="scope:delete")
        assert json.loads(line) == {
            "action": "approve",
            "message": "risky",
            "rule_key": "scope:delete",
        }

    def test_whitespace_only_omitted(self):
        line = format_approve_directive(message="  ", rule_key="\t")
        assert json.loads(line) == {"action": "approve"}
