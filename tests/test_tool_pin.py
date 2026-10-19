"""Tests for tool-prefix pinning behavior."""

import json

from scripts.tool_pin import (
    _agent_tool_names,
    _merge_preserving_prefix,
    annotate_inert_tools,
    persist_tool_names,
    pin_tool_prefix,
)


class DummyAgent:
    def __init__(self, tools=None):
        self.tools = tools or []
        self._surface_switch_note = ""


def make_tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_agent_tool_names_extracts_names():
    agent = DummyAgent([make_tool("a"), make_tool("b")])
    assert _agent_tool_names(agent) == ["a", "b"]


def test_agent_tool_names_handles_missing():
    agent = DummyAgent([{"type": "function", "function": {}}])
    assert _agent_tool_names(agent) == []


def test_merge_preserving_prefix_keeps_order_and_appends_new():
    pinned = ["a", "b"]
    built = ["b", "c", "d"]
    merged = _merge_preserving_prefix(pinned, built)
    assert merged == ["a", "b", "c", "d"]


def test_merge_preserving_prefix_no_duplicates():
    pinned = ["a", "b"]
    built = ["a", "b"]
    merged = _merge_preserving_prefix(pinned, built)
    assert merged == ["a", "b"]


def test_pin_tool_prefix_restores_and_merges():
    agent = DummyAgent([make_tool("c"), make_tool("d")])  # tui builds c,d
    session_row = {"tool_names": json.dumps(["a", "b"])}  # desktop had a,b

    restored = pin_tool_prefix(agent, session_row)
    assert restored is True
    # pinned prefix a,b preserved; c,d appended
    assert agent._pinned_tool_names == ["a", "b", "c", "d"]
    # a,b are inert on tui
    assert agent._inert_tool_names == ["a", "b"]


def test_pin_tool_prefix_no_session_returns_false():
    agent = DummyAgent()
    assert pin_tool_prefix(agent, None) is False
    assert pin_tool_prefix(agent, {}) is False


def test_annotate_inert_tools_appends_note():
    agent = DummyAgent()
    agent._inert_tool_names = ["focus_pane", "click"]
    agent._surface_switch_note = "Surface switched to tui."

    annotate_inert_tools(agent)

    assert "focus_pane" in agent._surface_switch_note
    assert "click" in agent._surface_switch_note
    assert "cached request prefix" in agent._surface_switch_note


def test_annotate_inert_tools_noop_when_empty():
    agent = DummyAgent()
    agent._inert_tool_names = []
    agent._surface_switch_note = "note"

    annotate_inert_tools(agent)

    assert agent._surface_switch_note == "note"


def test_persist_tool_names_returns_current_wire_order():
    agent = DummyAgent([make_tool("x"), make_tool("y")])
    assert persist_tool_names(agent) == ["x", "y"]
