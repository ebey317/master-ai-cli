"""
Shell hook response parser for pre_tool_call events.

Portable design from NousResearch/hermes-agent#30b38d1:
- Parses Hermes canonical {"action": "approve", "message"?, "rule_key"?}
- Strips and keeps only non-empty optional fields
- Does NOT map {"decision": "approve"} (that dialect means auto-allow)
- Returns None for unrecognized / malformed payloads so the tool proceeds
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


def parse_pre_tool_call_response(stdout: str) -> Optional[Dict[str, Any]]:
    """
    Parse a shell hook's stdout for a pre_tool_call directive.

    Returns a directive dict or None. Recognized shapes (Hermes canonical first):
      {"action": "approve", "message": "...", "rule_key": "..."}  -> escalate to approval gate
      {"action": "block", "message": "..."}                       -> block with message
      {"action": "modify", "args": {...}}                         -> modify tool args
      {"decision": "block", "reason": "..."}                      -> Claude-Code block
      {"decision": "modify", "tool_input": {...}}                 -> Claude-Code modify
    """
    stdout = stdout.strip()
    if not stdout:
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    # Hermes-only escalation to human-approval gate.
    # Claude-Code's {"decision": "approve"} means auto-ALLOW, so it is deliberately not mapped.
    if data.get("action") == "approve":
        directive: Dict[str, Any] = {"action": "approve"}
        for key in ("message", "rule_key"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                directive[key] = value.strip()
        return directive

    # Hermes canonical block
    if data.get("action") == "block":
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return {"action": "block", "message": message.strip()}
        return {"action": "block"}

    # Hermes canonical modify
    if data.get("action") == "modify":
        args = data.get("args")
        if isinstance(args, dict):
            return {"action": "modify", "args": args}

    # Claude-Code dialect: decision:block
    if data.get("decision") == "block":
        reason = data.get("reason")
        if isinstance(reason, str) and reason.strip():
            return {"action": "block", "message": reason.strip()}
        return {"action": "block"}

    # Claude-Code dialect: decision:modify
    if data.get("decision") == "modify":
        tool_input = data.get("tool_input")
        if isinstance(tool_input, dict):
            return {"action": "modify", "args": tool_input}

    # decision:approve means auto-allow — do not map to approval gate
    return None
