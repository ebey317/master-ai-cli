#!/usr/bin/env python3
"""Unified Sensei MCP server (stdio JSON-RPC -> local bridge HTTP).

This server is intentionally minimal:
- JSON-RPC over stdio
- Flat tool schemas for small local models
- Bridge contract aligned to /extension/queue actions[] payloads
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BRIDGE = "http://127.0.0.1:8080"
PROTO_VERSION = "2024-11-05"
SERVER_NAME = "sensei-mcp"
SERVER_VERSION = "2026.05.22-master"
DEFAULT_SESSION = "mcp-default"
WAIT_SECONDS = 8.0
POLL_MS = 300


def _http(method: str, path: str, body: dict | None = None, timeout: float = 5.0) -> dict:
    url = BRIDGE + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="replace")
            try:
                return {"ok": True, "status": res.status, "json": json.loads(raw)}
            except Exception:
                return {"ok": True, "status": res.status, "text": raw}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": str(e)}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)}


def _bridge_alive() -> bool:
    h = _http("GET", "/health", timeout=1.5)
    if h.get("ok"):
        return True
    q = _http("GET", "/extension/queue_state", timeout=1.5)
    return bool(q.get("ok"))


def _known_sessions(primary: str) -> list[str]:
    out: list[str] = []
    for sid in [primary, DEFAULT_SESSION, "chat-default"]:
        s = str(sid or "").strip()
        if s and s not in out:
            out.append(s)
    r = _http("GET", "/extension/sessions", timeout=2.0)
    if r.get("ok"):
        j = r.get("json") or {}
        active = str(j.get("active_side_panel_session") or "").strip()
        if active and active not in out:
            out.append(active)
        for sid in j.get("known_sessions") or []:
            s = str(sid or "").strip()
            if s and s not in out:
                out.append(s)
    return out


def _action_id(push_json: dict) -> str | None:
    aid = push_json.get("action_id")
    if isinstance(aid, str) and aid:
        return aid
    aids = push_json.get("action_ids")
    if isinstance(aids, list) and aids and isinstance(aids[0], str):
        return aids[0]
    return None


def _wait_for_result(action_id: str, wait_seconds: float) -> dict:
    deadline = time.time() + wait_seconds
    last_json = None
    while time.time() < deadline:
        q = urllib.parse.quote(action_id, safe="")
        r = _http("GET", f"/extension/result?action_id={q}", timeout=2.0)
        if r.get("ok"):
            j = r.get("json") or {}
            last_json = j
            if j.get("result") or j.get("ready") is True:
                return {"ok": True, "status": "completed", "result": j}
        elif r.get("status") not in (0, 404):
            return {"ok": False, "status": "error", "error": r.get("error"), "http_status": r.get("status")}
        time.sleep(POLL_MS / 1000.0)
    return {"ok": False, "status": "pending", "reason": "timeout", "last": last_json}


def _dispatch(action: dict, session_id: str, wait_seconds: float) -> dict:
    if not _bridge_alive():
        return {
            "ok": False,
            "status": "bridge_unreachable",
            "hint": "Open Chrome and the Sensei side panel, then retry.",
        }
    attempts: list[dict] = []
    for sid in _known_sessions(session_id):
        push = _http(
            "POST",
            "/extension/queue",
            body={"session_id": sid, "actions": [action]},
            timeout=4.0,
        )
        if not push.get("ok"):
            attempts.append({"session_id": sid, "status": "push_failed", "detail": push})
            continue
        push_json = push.get("json") or {}
        aid = _action_id(push_json)
        if not aid:
            attempts.append({"session_id": sid, "status": "push_no_action_id", "detail": push_json})
            continue
        outcome = _wait_for_result(aid, wait_seconds=wait_seconds)
        attempts.append({"session_id": sid, "action_id": aid, "status": outcome.get("status")})
        if outcome.get("ok"):
            return {
                "ok": True,
                "status": "completed",
                "session_id": sid,
                "action_id": aid,
                "outcome": outcome.get("result"),
                "attempts": attempts,
            }
    return {"ok": False, "status": "pending", "attempts": attempts}


def _to_wait_seconds(raw: object, default: float = WAIT_SECONDS) -> float:
    try:
        if raw is None:
            return default
        return max(0.5, float(str(raw).strip()))
    except Exception:
        return default


def _to_max_bytes(raw: object, default: int = 65536) -> int:
    try:
        if raw is None:
            return default
        return max(256, int(str(raw).strip()))
    except Exception:
        return default


def _ok_payload(payload: dict) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
        "isError": False,
    }


def _err_payload(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


TOOLS = [
    {
        "name": "sensei.health",
        "description": "Check whether the local Sensei bridge is reachable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sensei.chat",
        "description": "Send a natural-language browser task to the Sensei bridge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task to execute in the browser."},
                "session_id": {"type": "string", "description": "Bridge session id."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "browser.navigate",
        "description": "Navigate a browser tab to a URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open."},
                "session_id": {"type": "string", "description": "Bridge session id."},
                "wait_seconds": {"type": "string", "description": "How long to wait for action result."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser.click",
        "description": "Click a target element in the active page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Target selector or text anchor."},
                "session_id": {"type": "string", "description": "Bridge session id."},
                "wait_seconds": {"type": "string", "description": "How long to wait for action result."},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser.fill",
        "description": "Fill an input field with a value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Input selector."},
                "value": {"type": "string", "description": "Text value to type."},
                "session_id": {"type": "string", "description": "Bridge session id."},
                "wait_seconds": {"type": "string", "description": "How long to wait for action result."},
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "browser.read_local",
        "description": "Read a local file through the bridge safe-reader endpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute local path under home."},
                "max_bytes": {"type": "string", "description": "Maximum bytes to read."},
            },
            "required": ["path"],
        },
    },
]


def _handle_tool(name: str, args: dict) -> dict:
    if name == "sensei.health":
        health = _http("GET", "/health", timeout=2.0)
        queue = _http("GET", "/extension/queue_state", timeout=2.0)
        return _ok_payload({"bridge_health": health, "queue_state": queue})

    if name == "sensei.chat":
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return _err_payload("sensei.chat: prompt is required")
        sid = str(args.get("session_id") or DEFAULT_SESSION)
        body = {
            "prompt": prompt,
            "session_id": sid,
            "mode": "auto",
            "source": "mcp",
            "page_context": {},
        }
        res = _http("POST", "/chat", body=body, timeout=90.0)
        if not res.get("ok"):
            return _err_payload(f"sensei.chat bridge error: {res.get('error')}")
        return _ok_payload(res.get("json") or {"status": "ok"})

    if name == "browser.navigate":
        url = str(args.get("url") or "").strip()
        if not url:
            return _err_payload("browser.navigate: url is required")
        if "://" not in url:
            url = "https://" + url
        sid = str(args.get("session_id") or DEFAULT_SESSION)
        wait_seconds = _to_wait_seconds(args.get("wait_seconds"))
        action = {"kind": "BROWSER_NAV", "target": url}
        return _ok_payload(_dispatch(action, sid, wait_seconds))

    if name == "browser.click":
        selector = str(args.get("selector") or "").strip()
        if not selector:
            return _err_payload("browser.click: selector is required")
        sid = str(args.get("session_id") or DEFAULT_SESSION)
        wait_seconds = _to_wait_seconds(args.get("wait_seconds"))
        action = {"kind": "BROWSER_CLICK", "target": selector}
        return _ok_payload(_dispatch(action, sid, wait_seconds))

    if name == "browser.fill":
        selector = str(args.get("selector") or "").strip()
        value = str(args.get("value") or "")
        if not selector:
            return _err_payload("browser.fill: selector is required")
        sid = str(args.get("session_id") or DEFAULT_SESSION)
        wait_seconds = _to_wait_seconds(args.get("wait_seconds"))
        action = {"kind": "BROWSER_FILL", "target": selector, "value": value}
        return _ok_payload(_dispatch(action, sid, wait_seconds))

    if name == "browser.read_local":
        path = str(args.get("path") or "").strip()
        if not path:
            return _err_payload("browser.read_local: path is required")
        max_bytes = _to_max_bytes(args.get("max_bytes"))
        r = _http(
            "POST",
            "/extension/read_local_file",
            body={"path": path, "max_bytes": max_bytes},
            timeout=10.0,
        )
        if not r.get("ok"):
            return _err_payload(f"browser.read_local bridge error: {r.get('error')}")
        return _ok_payload(r.get("json") or {"status": "ok"})

    return _err_payload(f"unknown tool: {name}")


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _handle(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTO_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        return {"jsonrpc": "2.0", "id": req_id, "result": _handle_tool(str(name or ""), args)}

    if req_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"[sensei-mcp] parse error: {e}\n")
            sys.stderr.flush()
            continue
        try:
            resp = _handle(req)
        except Exception as e:
            sys.stderr.write(f"[sensei-mcp] handler crash: {e}\n")
            sys.stderr.flush()
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "error": {"code": -32603, "message": f"internal error: {e}"},
            }
        if resp is not None:
            _send(resp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
