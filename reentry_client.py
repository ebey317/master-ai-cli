#!/usr/bin/env python3
"""
reentry_client.py — thin subprocess MCP client for reentry-desk.
Spawns server.py via stdio JSON-RPC and calls tools synchronously.
Used by sensei_bridge.py to expose /reentry/* HTTP routes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

SERVER_CMD = ["python3", str(Path.home() / "projects/reentry-desk/server.py")]
TIMEOUT = 15.0  # seconds per call


def _rpc(method: str, params: dict) -> dict:
    """
    Spawn server.py, send initialize + one method call over stdio JSON-RPC,
    read the result, then let the process close. Each call is stateless.
    """
    init_msg = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "reentry_client", "version": "0.1"},
        }
    }) + "\n"

    # MCP requires notifications/initialized after initialize response
    notif_msg = json.dumps({
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}
    }) + "\n"

    call_id = 3
    call_msg = json.dumps({
        "jsonrpc": "2.0", "id": call_id, "method": method, "params": params
    }) + "\n"

    try:
        proc = subprocess.Popen(
            SERVER_CMD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = proc.communicate(
            input=(init_msg + notif_msg + call_msg).encode(),
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"error": "reentry-desk server timeout"}
    except FileNotFoundError:
        return {"error": "reentry-desk server not found — check SERVER_CMD"}

    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == call_id:  # id=3, the actual tool call
            if "error" in obj:
                return {"error": obj["error"]}
            return obj.get("result", {})

    return {"error": "no response from reentry-desk server", "stderr": stderr.decode(errors="replace")}


# ---------------------------------------------------------------------------
# Tool wrappers
# ---------------------------------------------------------------------------

def create_client(name: str, contact_email: str = "", phone: str = "", notes: str = "") -> dict:
    return _rpc("tools/call", {
        "name": "create_client",
        "arguments": {"name": name, "contact_email": contact_email, "phone": phone, "notes": notes},
    })


def get_client(client_id: str) -> dict:
    return _rpc("tools/call", {"name": "get_client", "arguments": {"client_id": client_id}})


def list_forms(client_id: str) -> dict:
    return _rpc("tools/call", {"name": "list_forms", "arguments": {"client_id": client_id}})


def fill_form(client_id: str, form_name: str) -> dict:
    return _rpc("tools/call", {"name": "fill_form", "arguments": {"client_id": client_id, "form_name": form_name}})


def mark_complete(client_id: str, form_name: str) -> dict:
    return _rpc("tools/call", {"name": "mark_complete", "arguments": {"client_id": client_id, "form_name": form_name}})


def get_status(client_id: str) -> dict:
    return _rpc("tools/call", {"name": "get_status", "arguments": {"client_id": client_id}})


def _text_from_result(result: dict) -> str:
    """Extract plain text from a FastMCP tool result."""
    if "error" in result:
        return f"[reentry-desk error] {result['error']}"
    content = result.get("content", [])
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(result)


# ---------------------------------------------------------------------------
# Convenience: call by name
# ---------------------------------------------------------------------------

TOOL_MAP = {
    "create_client": create_client,
    "get_client": get_client,
    "list_forms": list_forms,
    "fill_form": fill_form,
    "mark_complete": mark_complete,
    "get_status": get_status,
}


def call(tool_name: str, **kwargs) -> str:
    """Call a reentry-desk tool by name and return plain text."""
    fn = TOOL_MAP.get(tool_name)
    if not fn:
        return f"[reentry-desk] Unknown tool: {tool_name}. Available: {list(TOOL_MAP)}"
    return _text_from_result(fn(**kwargs))


if __name__ == "__main__":
    # Quick smoke test
    print(call("create_client", name="Test User", contact_email="test@example.com"))
    print(call("get_status", client_id="test_user"))
    print(call("list_forms", client_id="test_user"))
