#!/usr/bin/env python3
"""sensei_mcp_server.py — MCP stdio server that fronts the Sensei bridge.

Speaks JSON-RPC 2.0 over newline-delimited stdio per the Model Context
Protocol. Exposes the Sensei browser stack as MCP tools so external
clients (Claude Code, Cline, etc.) can drive the local Chrome extension
without any account dependency.

Protocol methods implemented:
  - initialize
  - notifications/initialized
  - tools/list
  - tools/call

Tools exposed (each forwards to http://127.0.0.1:8080):
  - sensei.chat          : run a prompt, get BROWSER_* directives back
  - sensei.health        : ping the bridge
  - browser.navigate     : ask the model to navigate to a URL (returns directives)
  - browser.click        : ask the model to click a selector
  - browser.fill         : ask the model to fill a selector with a value
  - browser.read_local   : safely read a local file from the bridge
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error


BRIDGE = "http://127.0.0.1:8080"
PROTO_VERSION = "2024-11-05"
SERVER_NAME = "sensei-mcp"
SERVER_VERSION = "0.1.0"


def _post(path: str, body: dict, timeout: float = 120.0) -> dict:
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE + path,
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _get(path: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(BRIDGE + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _wait_for_result(action_id: str, wait_seconds: float = 15.0, poll_ms: int = 500) -> dict:
    """Short-poll the bridge for a browser action's outcome. Returns
    {ok, action_id, status, result?} — `status` is 'completed' or 'pending'.
    Used by browser.navigate/click/fill so the MCP caller sees Chrome's
    actual result inline, not just 'I queued it'."""
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            res = _get(f"/extension/result?action_id={action_id}", timeout=3.0)
            if res.get("ok") and res.get("result"):
                return {"ok": True, "action_id": action_id, "status": "completed", **res}
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return {"ok": False, "action_id": action_id, "status": "error", "error": str(e)}
        except Exception as e:
            return {"ok": False, "action_id": action_id, "status": "error", "error": str(e)}
        time.sleep(poll_ms / 1000.0)
    return {"ok": False, "action_id": action_id, "status": "pending",
            "hint": "no result within wait window — is the Chrome extension side panel open and polling?"}


TOOLS = [
    {
        "name": "sensei.chat",
        "description": "Send a prompt to the Sensei bridge. The local model returns BROWSER_* directives the Chrome extension should execute (navigate, click, fill, read, upload, submit). Use this as the high-level entry point for browser automation goals.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Goal in natural language."},
                "session_id": {"type": "string", "description": "Optional session id for multi-turn context."},
                "page_context": {"type": "object", "description": "Optional {url,title,text,ax_snapshot}."},
                "mode": {"type": "string", "enum": ["review", "auto", "plan", "quick"], "default": "auto"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "sensei.health",
        "description": "Health check — confirms the bridge and Ollama are reachable. Returns {ok, model, vision_model}.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser.navigate",
        "description": "Convenience wrapper — asks the model to emit a BROWSER_NAV directive for the given URL.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser.click",
        "description": "Convenience wrapper — asks the model to emit a BROWSER_CLICK directive for the given CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "browser.fill",
        "description": "Convenience wrapper — asks the model to emit a BROWSER_FILL directive for the given selector + value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "browser.read_local",
        "description": "Read a local file under $HOME via the bridge's safety-fenced reader. Refuses paths in ~/.ssh, ~/.gnupg, credentials.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "default": 65536},
            },
            "required": ["path"],
        },
    },
]


def _call_tool(name: str, args: dict) -> dict:
    args = args or {}
    if name == "sensei.health":
        return _get("/health")
    if name == "sensei.chat":
        body = {
            "prompt": args.get("prompt", ""),
            "session_id": args.get("session_id", "mcp-default"),
            "page_context": args.get("page_context") or {},
            "mode": args.get("mode") or "auto",
            "source": "mcp",
        }
        return _post("/chat", body)
    # browser.navigate/click/fill push structured actions DIRECTLY to the
    # bridge queue (no model call), then short-poll /extension/result so
    # the caller sees Chrome's actual outcome inline — not a fake "queued."
    # If the operator's Chrome extension isn't polling, the result will
    # honestly time out and return status=pending with a hint.
    if name in ("browser.navigate", "browser.click", "browser.fill"):
        sid = args.get("session_id") or "mcp-default"
        wait_s = float(args.get("wait_seconds", 15))
        if name == "browser.navigate":
            action = {"kind": "BROWSER_NAV", "target": args.get("url", "")}
        elif name == "browser.click":
            action = {"kind": "BROWSER_CLICK", "target": args.get("selector", "")}
        else:
            action = {"kind": "BROWSER_FILL", "target": args.get("selector", ""),
                      "value": args.get("value", "")}
        push = _post("/extension/queue", {"session_id": sid, "actions": [action]})
        action_id = (push.get("action_ids") or [None])[0]
        if not action_id:
            return {"ok": False, "push": push, "error": "no action_id returned"}
        # Short-poll for the Chrome outcome.
        outcome = _wait_for_result(action_id, wait_seconds=wait_s)
        return {"ok": outcome.get("ok", False), "session_id": sid,
                "action": action, "action_id": action_id, "outcome": outcome,
                "push": push}
    if name == "browser.read_local":
        return _post("/extension/read_local_file", {
            "path": args.get("path", ""),
            "max_bytes": args.get("max_bytes", 65536),
        })
    raise ValueError(f"unknown tool {name}")


def _result_envelope(payload: dict) -> dict:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, separators=(",", ":"))}
        ],
        "isError": False,
    }


def _error_envelope(msg: str) -> dict:
    return {
        "content": [{"type": "text", "text": msg}],
        "isError": True,
    }


def _handle(msg: dict) -> dict | None:
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTO_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "notifications/initialized":
        return None  # notifications have no response

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            payload = _call_tool(name, args)
            return {"jsonrpc": "2.0", "id": msg_id, "result": _result_envelope(payload)}
        except urllib.error.URLError as e:
            return {"jsonrpc": "2.0", "id": msg_id, "result": _error_envelope(f"bridge unreachable: {e}")}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": msg_id, "result": _error_envelope(f"{type(e).__name__}: {e}")}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return None


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"[mcp] parse error: {e}\n")
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
