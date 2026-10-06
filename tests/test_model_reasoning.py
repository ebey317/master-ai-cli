"""
Tests for reasoning effort selection on model pickers.
"""

import argparse

import pytest

from scripts.model_reasoning import (
    ModelSwitchRequest,
    ReasoningEffort,
    add_reasoning_arg,
    apply_reasoning_effort,
    build_reasoning_picker_rows,
    model_supports_reasoning,
)


class TestReasoningEffortParsing:
    def test_valid_levels(self):
        assert ReasoningEffort.parse("low") == ReasoningEffort.LOW
        assert ReasoningEffort.parse("medium") == ReasoningEffort.MEDIUM
        assert ReasoningEffort.parse("high") == ReasoningEffort.HIGH
        assert ReasoningEffort.parse("xhigh") == ReasoningEffort.VERY_HIGH
        assert ReasoningEffort.parse("none") == ReasoningEffort.NONE

    def test_unicode_dash_normalization(self):
        assert ReasoningEffort.parse("high") == ReasoningEffort.parse("high")
        assert ReasoningEffort.parse("\u2013high") == ReasoningEffort.HIGH  # en-dash
        assert ReasoningEffort.parse("\u2014high") == ReasoningEffort.HIGH  # em-dash

    def test_invalid_level_raises(self):
        with pytest.raises(ValueError) as exc:
            ReasoningEffort.parse("invalid")
        assert "Invalid reasoning effort" in str(exc.value)


class TestModelSwitchRequest:
    def test_request_carries_reasoning(self):
        req = ModelSwitchRequest(
            model="gpt-4",
            provider="openai",
            reasoning_effort=ReasoningEffort.HIGH,
            scope="session",
        )
        assert req.reasoning_effort == ReasoningEffort.HIGH
        assert req.scope == "session"


class TestReasoningPickerRows:
    def test_rows_include_all_levels(self):
        rows = build_reasoning_picker_rows()
        values = [r[0] for r in rows]
        assert "none" in values
        assert "low" in values
        assert "medium" in values
        assert "high" in values
        assert "xhigh" in values

    def test_rows_include_keep_current(self):
        rows = build_reasoning_picker_rows(current="high")
        values = [r[0] for r in rows]
        assert "high" in values
        labels = [r[1] for r in rows]
        assert any("Keep current" in l for l in labels)


class TestCapabilityGate:
    def test_supports_reasoning_by_default(self):
        assert model_supports_reasoning({}) is True
        assert model_supports_reasoning({"supports_reasoning": True}) is True

    def test_disabled_when_false(self):
        assert model_supports_reasoning({"supports_reasoning": False}) is False


class TestArgumentParserIntegration:
    def test_parser_has_reasoning_flag(self):
        parser = argparse.ArgumentParser()
        add_reasoning_arg(parser)
        args = parser.parse_args(["--reasoning", "high"])
        assert args.reasoning == "high"

    def test_parser_rejects_invalid(self):
        parser = argparse.ArgumentParser()
        add_reasoning_arg(parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["--reasoning", "invalid"])


class TestApplyReasoningEffort:
    def test_session_scope(self, monkeypatch):
        calls = []
        monkeypatch.setattr("sensei.config.set", lambda k, v: calls.append((k, v)))
        monkeypatch.setattr("sensei.config.get", lambda k, d=None: d)

        msg = apply_reasoning_effort("session-1", ReasoningEffort.HIGH, scope="session")
        assert "session" in msg
        assert ("session.reasoning_override", "high") in calls

    def test_global_scope(self, monkeypatch):
        calls = []
        monkeypatch.setattr("sensei.config.set", lambda k, v: calls.append((k, v)))

        msg = apply_reasoning_effort("session-1", ReasoningEffort.HIGH, scope="global")
        assert "global" in msg
        assert ("agent.reasoning_effort", "high") in calls

    def test_once_scope_snapshots_restore(self, monkeypatch):
        calls = []
        monkeypatch.setattr("sensei.config.set", lambda k, v: calls.append((k, v)))
        monkeypatch.setattr(
            "sensei.config.get",
            lambda k, d=None: "medium" if k == "agent.reasoning_effort" else d,
        )

        msg = apply_reasoning_effort("session-1", ReasoningEffort.HIGH, scope="once")
        assert "one turn" in msg
        assert ("session.reasoning_override", "high") in calls
        assert ("session.reasoning_restore", "medium") in calls
