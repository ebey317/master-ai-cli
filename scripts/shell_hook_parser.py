"""
Portable shell hook parser for pre_tool_call events.

Implements the Hermes-inspired design where shell hooks can emit:
  - {"action": "block", "message": "..."}          -> block the tool
  - {"action": "modify", "args": {...}}            -> modify tool args
  - {"action": "approve", "message"?, "rule_key"?} -> escalate to human approval gate

Claude-Code's `{"decision": "approve"}` means auto-ALLOW and is deliberately NOT mapped.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


# Dialects ordered by precedence (first match wins)
_PRE_TOOL_DIALECTS = [
    # (verb_key, expected_value, _, payload_key)
    ("action", "block", None, "message"),
    ("action", "modify", None, "args"),
    ("decision", "block", None, "reason"),
    ("decision", "modify", None, "tool_input"),
]


def _parse_block_or_modify(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse block/modify dialects shared with Claude-Code compatibility."""
    for verb, expected, _, payload_key in _PRE_TOOL_DIALECTS:
        if data.get(verb) == expected:
            payload = data.get(payload_key)
            if isinstance(payload, dict) and payload_key == "args":
                return {"action": "modify", "args": payload}
            if isinstance(payload, dict) and payload_key == "tool_input":
                return {"action": "modify", "args": payload}
            if isinstance(payload, str) and payload_key in ("message", "reason"):
                return {"action": "block", "message": payload.strip()} if payload.strip() else {"action": "block"}
    return None


def parse_pre_tool_call(stdout: str) -> Optional[Dict[str, Any]]:
    """
    Parse a shell hook's stdout for pre_tool_call directives.

    Returns a canonical directive dict or None if no recognized directive.
    Recognized actions:
      - block:   {"action": "block", "message"?: str}
      - modify:  {"action": "modify", "args": Dict}
      - approve: {"action": "approve", "message"?: str, "rule_key"?: str}

    The `approve` action escalates to the human approval gate.
    Optional fields are included only when non-empty stripped strings.
    """
    if not stdout or not stdout.strip():
        return None

    try:
        data = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    # 1. Block / modify dialects (Claude-Code compatible + Hermes canonical)
    directive = _parse_block_or_modify(data)
    if directive:
        return directive

    # 2. Hermes-only: escalate to human approval gate
    #    Claude-Code's `{"decision": "approve"}` means auto-ALLOW and is NOT mapped here.
    if data.get("action") == "approve":
        directive: Dict[str, Any] = {"action": "approve"}
        for key in ("message", "rule_key"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                directive[key] = value.strip()
        return directive

    return None


def parse_post_tool_call(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse a shell hook's stdout for post_tool_call directives (future extensibility)."""
    if not stdout or not stdout.strip():
        return None
    try:
        data = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Reserved for future: context injection, notifications, etc.
    return None


def parse_pre_llm_call(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse a shell hook's stdout for pre_llm_call directives."""
    if not stdout or not stdout.strip():
        return None
    try:
        data = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Hermes canonical: {"context": "..."}
    if "context" in data and isinstance(data["context"], str):
        ctx = data["context"].strip()
        if ctx:
            return {"context": ctx}
    return None
