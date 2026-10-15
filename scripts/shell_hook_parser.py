"""Parse and validate shell hook responses for pre_tool_call events.

Portable design from NousResearch/hermes-agent#30b38d1:
- "action": "approve" → escalate to human approval gate (carries message, rule_key)
- "action": "block"   → deny with message
- "action": "modify"  → rewrite tool arguments
- "decision": "approve" (Claude-Code) → auto-allow, NOT mapped to human gate
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


def parse_pre_tool_call(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse a shell hook's stdout for pre_tool_call directives.

    Returns a canonical directive dict or None if no recognized directive.
    Recognized shapes (Hermes-canonical first, then Claude-Code compat):
      {"action": "approve", "message"?, "rule_key"?}  → escalate to human gate
      {"action": "block", "message"?}                 → deny tool
      {"action": "modify", "args": {...}}             → rewrite args
      {"decision": "block", "reason"?}                → deny (CC compat)
      {"decision": "modify", "tool_input": {...}}     → rewrite args (CC compat)

    Notes:
    - "decision": "approve" means auto-allow in Claude-Code; deliberately NOT mapped.
    - Optional string fields (message, reason, rule_key) are stripped and omitted if empty.
    - Invalid JSON or unrecognized top-level keys → None (hook treated as no-op).
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

    # Hermes-canonical: approve → human approval gate
    if data.get("action") == "approve":
        directive: Dict[str, Any] = {"action": "approve"}
        for key in ("message", "rule_key"):
            value = data.get(key)
            if isinstance(value, str):
                stripped = value.strip()
                if stripped:
                    directive[key] = stripped
        return directive

    # Hermes-canonical: block
    if data.get("action") == "block":
        directive = {"action": "block"}
        msg = data.get("message")
        if isinstance(msg, str):
            stripped = msg.strip()
            if stripped:
                directive["message"] = stripped
        return directive

    # Hermes-canonical: modify
    if data.get("action") == "modify" and isinstance(data.get("args"), dict):
        return {"action": "modify", "args": data["args"]}

    # Claude-Code compat: decision:block → block
    if data.get("decision") == "block":
        directive = {"action": "block"}
        reason = data.get("reason")
        if isinstance(reason, str):
            stripped = reason.strip()
            if stripped:
                directive["message"] = stripped
        return directive

    # Claude-Code compat: decision:modify → modify
    if data.get("decision") == "modify" and isinstance(data.get("tool_input"), dict):
        return {"action": "modify", "args": data["tool_input"]}

    # decision:approve is auto-allow in Claude-Code; not mapped to human gate
    return None


def format_approve_directive(message: str = "", rule_key: str = "") -> str:
    """Build a JSON line for a shell hook to emit an approve escalation."""
    payload: Dict[str, Any] = {"action": "approve"}
    if message.strip():
        payload["message"] = message.strip()
    if rule_key.strip():
        payload["rule_key"] = rule_key.strip()
    return json.dumps(payload, separators=(",", ":"))
