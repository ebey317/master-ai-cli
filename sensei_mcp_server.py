#!/usr/bin/env python3
"""
sensei_mcp_server.py — Sensei MCP for Claude Code (secretary-mode).

Six tools: chat, browse, click, fill, read, search.
3-tool limit only applies when local 7B is the brain. Max account = no limit.
JSON-RPC over stdio. Talks to the Sensei bridge at 127.0.0.1:8080.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

BRIDGE = "http://127.0.0.1:8080"
DEFAULT_SESSION = "mcp-default"
WAIT_SECONDS = 8
POLL_MS = 400


# ---------------------------------------------------------------------------
# bridge helpers
# ---------------------------------------------------------------------------

def _http(method, path, body=None, timeout=5.0):
    url = f"{BRIDGE}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return {"ok": True, "status": resp.status, "json": json.loads(raw)}
            except Exception:
                return {"ok": True, "status": resp.status, "text": raw}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": str(e)}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)}


def _bridge_alive():
    r = _http("GET", "/extension/queue_state", timeout=1.0)
    return bool(r.get("ok"))


def _push(action, session=DEFAULT_SESSION):
    body = {"session_id": session, "actions": [action]}
    return _http("POST", "/extension/queue", body=body, timeout=3.0)


def _await_result(action_id, session=DEFAULT_SESSION, wait_seconds=WAIT_SECONDS):
    """Poll the bridge for a completed result. Returns the result dict or a
    timeout shape. Does not raise."""
    if not action_id:
        return {"ok": False, "reason": "no_action_id"}
    deadline = time.time() + wait_seconds
    last = None
    while time.time() < deadline:
        r = _http(
            "GET",
            f"/extension/result?session_id={session}&action_id={action_id}",
            timeout=2.0,
        )
        if r.get("ok") and r.get("json"):
            j = r["json"]
            if j.get("ready") or j.get("result") or j.get("action_id"):
                return j
            last = j
        time.sleep(POLL_MS / 1000.0)
    return {"ok": False, "reason": "timeout", "last": last}


def _action_id_from_push(push_response):
    """The bridge has shipped two shapes historically:
       - { action_id: "..." }
       - { action_ids: ["..."] }
       Accept either and return the single id or None."""
    if not push_response or not push_response.get("ok"):
        return None
    j = push_response.get("json") or {}
    aid = j.get("action_id")
    if isinstance(aid, str) and aid:
        return aid
    aids = j.get("action_ids")
    if isinstance(aids, list) and aids:
        return aids[0]
    return None


def _dispatch(kind, payload, session=DEFAULT_SESSION, wait=WAIT_SECONDS):
    """Push an action to the bridge and await the result. Returns a result
    dict shaped for the MCP content envelope."""
    if not _bridge_alive():
        return {"ok": False, "reason": "bridge_unreachable",
                "hint": "Open Chrome, click the Sensei icon, pin the side panel."}
    action = {"kind": kind, **payload}
    push = _push(action, session=session)
    if not push.get("ok"):
        return {"ok": False, "reason": "push_failed", "detail": push}
    aid = _action_id_from_push(push)
    result = _await_result(aid, session=session, wait_seconds=wait)
    return {"ok": result.get("ok", True), "result": result, "action_id": aid}


# ---------------------------------------------------------------------------
# tool handlers — each takes flat string parameters only
# ---------------------------------------------------------------------------

def tool_chat(args):
    msg = str(args.get("msg") or "")
    return {"content": [{"type": "text", "text": msg or "ok"}]}


def tool_browse(args):
    url = str(args.get("url") or "").strip()
    if not url:
        return {"content": [{"type": "text", "text": "browse: url is required"}]}
    if not url.startswith("http"):
        url = "https://" + url
    out = _dispatch("BROWSER_NAV", {"target": url})
    text = f"navigate {url} -> {json.dumps(out)[:600]}"
    return {"content": [{"type": "text", "text": text}]}


def tool_click(args):
    what = str(args.get("what") or "").strip()
    if not what:
        return {"content": [{"type": "text", "text": "click: what is required"}]}
    out = _dispatch("BROWSER_CLICK", {"target": what})
    text = f"click '{what}' -> {json.dumps(out)[:400]}"
    return {"content": [{"type": "text", "text": text}]}


def tool_fill(args):
    where = str(args.get("where") or "").strip()
    text = str(args.get("text") or "")
    if not where:
        return {"content": [{"type": "text", "text": "fill: where is required"}]}
    out = _dispatch("BROWSER_FILL", {"target": where, "value": text})
    rep = f"fill '{where}' = {text[:60]} -> {json.dumps(out)[:400]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_read(args):
    out = _dispatch("BROWSER_READ_PAGE", {})
    rep = json.dumps(out)
    if len(rep) > 500:
        rep = rep[:500] + " ...[truncated]"
    return {"content": [{"type": "text", "text": rep}]}


def tool_search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        return {"content": [{"type": "text", "text": "search: query is required"}]}
    url = "https://www.google.com/search?q=" + query.replace(" ", "+")
    out = _dispatch("BROWSER_NAV", {"target": url})
    rep = f"search '{query}' -> {json.dumps(out)[:400]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_run(args):
    cmd = str(args.get("cmd") or "").strip()
    if not cmd:
        return {"content": [{"type": "text", "text": "run: cmd is required"}]}
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        rep = out if out else err if err else "(no output)"
        if len(rep) > 800:
            rep = rep[:800] + " ...[truncated]"
        return {"content": [{"type": "text", "text": rep}]}
    except subprocess.TimeoutExpired:
        return {"content": [{"type": "text", "text": "run: timed out after 30s"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"run error: {e}"}]}


def tool_write_file(args):
    path = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    if not path:
        return {"content": [{"type": "text", "text": "write_file: path is required"}]}
    path = os.path.expanduser(path)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return {"content": [{"type": "text", "text": f"wrote {len(content)} chars to {path}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"write_file error: {e}"}]}


def tool_read_file(args):
    path = str(args.get("path") or "").strip()
    if not path:
        return {"content": [{"type": "text", "text": "read_file: path is required"}]}
    path = os.path.expanduser(path)
    try:
        with open(path, "r") as f:
            content = f.read(4000)
        return {"content": [{"type": "text", "text": content}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"read_file error: {e}"}]}


HANDLERS = {
    "chat": tool_chat,
    "browse": tool_browse,
    "click": tool_click,
    "fill": tool_fill,
    "read": tool_read,
    "search": tool_search,
    "run": tool_run,
    "write_file": tool_write_file,
    "read_file": tool_read_file,
}


# ---------------------------------------------------------------------------
# tool schemas — descriptions cut to a single short sentence each.
# every parameter is a flat string. no nested objects. no optional fields
# with defaults. no enums. nothing the 7B model can hallucinate the shape of.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "chat",
        "description": "Reply to the user with plain text.",
        "inputSchema": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    },
    {
        "name": "browse",
        "description": "Open a URL in the browser.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": "Click an element by visible label or selector.",
        "inputSchema": {
            "type": "object",
            "properties": {"what": {"type": "string"}},
            "required": ["what"],
        },
    },
    {
        "name": "fill",
        "description": "Type text into a field by label or selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "where": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["where", "text"],
        },
    },
    {
        "name": "read",
        "description": "Read the visible content of the current page.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "search",
        "description": "Search Google for a query.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "run",
        "description": "Run a shell command on this machine (CLI). Use for invoices, file ops, system tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
    {
        "name": "write_file",
        "description": "Write or create a file on this machine. Use for invoices, documents, notes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from this machine.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC stdio loop
# ---------------------------------------------------------------------------

def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _err(msg_id, code, message):
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sensei", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text",
                                            "text": f"unknown tool: {name}"}],
                               "isError": True}}
        try:
            result = handler(args if isinstance(args, dict) else {})
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text",
                                            "text": f"tool error: {e}"}],
                               "isError": True}}

    if mid is None:
        # notification we don't handle — silently drop
        return None

    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"[sensei] parse error: {e}\n")
            sys.stderr.flush()
            continue
        try:
            resp = _handle(msg)
        except Exception as e:
            sys.stderr.write(f"[sensei] handler crash: {e}\n")
            sys.stderr.flush()
            resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": f"internal: {e}"}}
        if resp is not None:
            _send(resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
