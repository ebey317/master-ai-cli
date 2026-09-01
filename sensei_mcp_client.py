"""Sensei MCP client — server catalog, JSON-RPC client, schema validation.

2026-09-01. Built so Sensei (master_ai.py) can act as an MCP CLIENT:
discover, register, enable/disable, and validate OTHER MCP servers, the
same way Hermes Agent's `hermes mcp` surface does for Hermes. Reference
implementations read before writing this:
  - ~/.hermes/config.yaml  mcp_servers: section (proven working shape)
  - ~/projects/master-ai/sensei_mcp_server.py (the stdio server Sensei
    itself exposes — JSON-RPC 2.0, protocolVersion 2024-11-05, supports
    both framed and newline-delimited messages)

Conventions follow this codebase:
  - State is a JSON file at ~/.master_ai_mcp/servers.json (matches the
    ~/.master_ai_schedules.json flat-JSON precedent; ~/.master_ai_keys is
    KEY=VALUE but holds secrets — this catalog holds no secrets, so JSON
    is the right fit and matches scheduler/hooks storage style).
  - stdlib only: subprocess, threading, queue, urllib, json, re, shutil.
  - chmod 0600 on the catalog (same hygiene as skill_runtime.save_state).

Transports (the two standard MCP kinds):
  - stdio: spawn the server process, newline-delimited JSON-RPC 2.0 over
    stdin/stdout (also speaks framed Content-Length servers, which send
    framed responses — we parse both).
  - sse: HTTP Server-Sent Events per the 2024-11-05 spec — GET the SSE
    endpoint, receive an `endpoint` event carrying the POST URL, POST
    JSON-RPC there, responses arrive as `message` events on the stream.

Validation gate (the task's hard requirement): add/enable PROBE the
server — initialize, tools/list, then check every tool has a well-formed
schema (name, description, inputSchema object with type=object). A server
that fails probing is stored but NOT enabled, with the reason recorded.
Nothing is trusted blindly.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

MCP_DIR = Path.home() / ".master_ai_mcp"
CATALOG_PATH = MCP_DIR / "servers.json"
LOG_PATH = MCP_DIR / "mcp_client.log"

PROTOCOL_VERSION = "2024-11-05"
CONNECT_TIMEOUT_S = 15.0
RPC_TIMEOUT_S = 20.0
TOOLS_CACHE_TTL_S = 300.0

VALID_TRANSPORTS = ("stdio", "sse")


def _log(msg: str) -> None:
    try:
        MCP_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except OSError:
        pass


# ─── Catalog storage ────────────────────────────────────────────────

def _load_catalog() -> dict:
    try:
        if not CATALOG_PATH.exists():
            return {"version": 1, "servers": {}}
        data = json.loads(CATALOG_PATH.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
            return {"version": 1, "servers": {}}
        data.setdefault("version", 1)
        return data
    except Exception as e:
        _log(f"CATALOG_LOAD_ERROR: {e}")
        return {"version": 1, "servers": {}}


def _save_catalog(cat: dict) -> None:
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cat, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CATALOG_PATH)


def get_server(name: str):
    return _load_catalog()["servers"].get(name)


def list_servers() -> dict:
    return _load_catalog()["servers"]


# ─── Tool-schema validation ─────────────────────────────────────────

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_tool_schema(tool) -> list:
    """Return a list of problems with an MCP tool descriptor.
    Empty list = well-formed. Checks name, description, inputSchema."""
    if not isinstance(tool, dict):
        return ["tool entry is not an object"]
    problems = []
    name = tool.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("missing/empty tool name")
    elif not _TOOL_NAME_RE.match(name):
        problems.append(f"invalid tool name {name!r} (allowed: letters, digits, _-, max 64)")
    desc = tool.get("description")
    if not isinstance(desc, str) or not desc.strip():
        problems.append(f"tool {name!r}: missing/empty description")
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        problems.append(f"tool {name!r}: inputSchema must be an object")
    else:
        if schema.get("type") != "object":
            problems.append(f"tool {name!r}: inputSchema.type must be 'object', got {schema.get('type')!r}")
        props = schema.get("properties")
        if props is not None and not isinstance(props, dict):
            problems.append(f"tool {name!r}: inputSchema.properties must be an object")
        req = schema.get("required")
        if req is not None and not (isinstance(req, list) and all(isinstance(r, str) for r in req)):
            problems.append(f"tool {name!r}: inputSchema.required must be a list of strings")
    return problems


def validate_tools(tools) -> dict:
    """Validate a tools/list result. Returns {ok: bool, problems: [...],
    tool_names: [...]}. A server with ANY malformed tool fails validation."""
    if not isinstance(tools, list) or not tools:
        return {"ok": False, "problems": ["tools/list returned no tools"], "tool_names": []}
    problems, names = [], []
    for t in tools:
        p = validate_tool_schema(t)
        if p:
            problems.extend(p)
        else:
            names.append(t.get("name"))
    return {"ok": not problems, "problems": problems, "tool_names": names}


# ─── JSON-RPC helpers ───────────────────────────────────────────────

def _rpc_error_free(resp) -> bool:
    return isinstance(resp, dict) and "error" not in resp and "result" in resp


# ─── stdio transport ────────────────────────────────────────────────

class StdioMcpClient:
    """One server process per client instance. JSON-RPC 2.0 over
    stdin/stdout, newline-delimited (framed responses parsed too).
    A reader thread pushes responses into a queue so we can timeout."""

    def __init__(self, command: str, args: list, env: Optional[dict] = None):
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.proc: Optional[subprocess.Popen] = None
        self._q = queue.Queue()
        self._reader = None
        self._id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        env = dict(os.environ)
        env.update(self.env)
        self.proc = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        """Parse both newline-delimited JSON and framed
        `Content-Length: N\\r\\n\\r\\n{...}` responses."""
        f = self.proc.stdout
        try:
            while True:
                line = f.readline()
                if not line:
                    break
                s = line.strip()
                if not s:
                    continue
                if s.lower().startswith("content-length:"):
                    # framed: read blank line, then N bytes
                    length = int(s.split(":", 1)[1].strip())
                    while True:
                        blank = f.readline()
                        if blank in ("", "\n", "\r\n"):
                            break
                    body = f.read(length)
                    try:
                        self._q.put(json.loads(body))
                    except Exception:
                        pass
                    continue
                if not s.startswith("{"):
                    continue  # non-JSON noise on stdout — ignore
                try:
                    self._q.put(json.loads(s))
                except Exception:
                    continue
        except Exception:
            pass
        self._q.put(None)  # EOF sentinel

    def _rpc(self, method: str, params: dict = None, timeout: float = RPC_TIMEOUT_S, notify: bool = False):
        with self._lock:
            self._id += 1
            msg_id = self._id
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = msg_id
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            return {"error": {"code": -1, "message": f"stdin write failed: {e}"}}
        if notify:
            return {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                resp = self._q.get(timeout=remaining)
            except queue.Empty:
                break
            if resp is None:  # EOF
                return {"error": {"code": -1, "message": "server closed stdout"}}
            if isinstance(resp, dict) and resp.get("id") == msg_id:
                return resp
            # notification or response to a different id — keep waiting
        return {"error": {"code": -2, "message": f"timeout waiting for {method} response ({timeout}s)"}}

    def probe(self) -> dict:
        """Full handshake: initialize → initialized → tools/list.
        Returns {"ok": bool, "tools": [...], "error": str, "server_info": dict}."""
        try:
            self.start()
        except Exception as e:
            return {"ok": False, "tools": [], "error": f"spawn failed: {e}", "server_info": {}}
        init = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "sensei", "version": "1.0.0"},
        }, timeout=CONNECT_TIMEOUT_S)
        if not _rpc_error_free(init):
            return {"ok": False, "tools": [], "server_info": {},
                    "error": f"initialize failed: {json.dumps(init.get('error', init))[:300]}"}
        server_info = (init.get("result") or {}).get("serverInfo") or {}
        self._rpc("notifications/initialized", {}, notify=True)
        tl = self._rpc("tools/list", {}, timeout=RPC_TIMEOUT_S)
        if not _rpc_error_free(tl):
            return {"ok": False, "tools": [], "server_info": server_info,
                    "error": f"tools/list failed: {json.dumps(tl.get('error', tl))[:300]}"}
        tools = (tl.get("result") or {}).get("tools") or []
        return {"ok": True, "tools": tools, "error": "", "server_info": server_info}

    def close(self) -> None:
        try:
            if self.proc and self.proc.stdin:
                self.proc.stdin.close()
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass


# ─── SSE transport ──────────────────────────────────────────────────

class SseMcpClient:
    """MCP SSE transport (2024-11-05 spec):
      1. GET <url> (Accept: text/event-stream) — a background thread reads
         the event stream.
      2. First event is `endpoint` — data is the POST URL for JSON-RPC
         (relative paths resolved against the SSE URL).
      3. POST JSON-RPC messages to that endpoint; responses arrive as
         `message` events on the SSE stream.
    """

    def __init__(self, url: str, headers: Optional[dict] = None):
        self.url = url
        self.headers = dict(headers or {})
        self._q = queue.Queue()
        self._post_url = None
        self._err = None
        self._started = threading.Event()
        self._thread = None
        self._id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._sse_loop, daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=CONNECT_TIMEOUT_S):
            raise RuntimeError(f"SSE connect failed: {self._err or 'timeout'}")

    def _sse_loop(self) -> None:
        try:
            req = urllib.request.Request(self.url, headers={
                "Accept": "text/event-stream", **self.headers})
            resp = urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_S)
            event, data_lines = "", []
            while True:
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if data_lines:
                        data = "\n".join(data_lines)
                        if event == "endpoint":
                            self._post_url = urllib.parse.urljoin(self.url, data)
                            self._started.set()
                        elif event in ("message", ""):
                            try:
                                self._q.put(json.loads(data))
                            except Exception:
                                pass
                    event, data_lines = "", []
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
        except Exception as e:
            self._err = str(e)
            self._started.set()

    def _post(self, msg: dict, timeout: float = RPC_TIMEOUT_S):
        if not self._post_url:
            return {"error": {"code": -1, "message": "no SSE endpoint received"}}
        data = json.dumps(msg).encode("utf-8")
        req = urllib.request.Request(self._post_url, data=data, headers={
            "Content-Type": "application/json", **self.headers})
        try:
            urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as e:
            return {"error": {"code": e.code, "message": f"POST failed: {e}"}}
        except Exception as e:
            return {"error": {"code": -1, "message": f"POST failed: {e}"}}
        return {}

    def _rpc(self, method: str, params: dict = None, timeout: float = RPC_TIMEOUT_S, notify: bool = False):
        with self._lock:
            self._id += 1
            msg_id = self._id
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = msg_id
        err = self._post(msg, timeout=timeout)
        if err:
            return err
        if notify:
            return {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                resp = self._q.get(timeout=remaining)
            except queue.Empty:
                break
            if isinstance(resp, dict) and resp.get("id") == msg_id:
                return resp
        return {"error": {"code": -2, "message": f"timeout waiting for {method} response ({timeout}s)"}}

    def probe(self) -> dict:
        try:
            self.start()
        except Exception as e:
            return {"ok": False, "tools": [], "server_info": {}, "error": str(e)}
        init = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "sensei", "version": "1.0.0"},
        }, timeout=CONNECT_TIMEOUT_S)
        if not _rpc_error_free(init):
            return {"ok": False, "tools": [], "server_info": {},
                    "error": f"initialize failed: {json.dumps(init.get('error', init))[:300]}"}
        server_info = (init.get("result") or {}).get("serverInfo") or {}
        self._rpc("notifications/initialized", {}, notify=True)
        tl = self._rpc("tools/list", {}, timeout=RPC_TIMEOUT_S)
        if not _rpc_error_free(tl):
            return {"ok": False, "tools": [], "server_info": server_info,
                    "error": f"tools/list failed: {json.dumps(tl.get('error', tl))[:300]}"}
        tools = (tl.get("result") or {}).get("tools") or []
        return {"ok": True, "tools": tools, "error": "", "server_info": server_info}

    def close(self) -> None:
        pass  # reader thread is daemon; nothing persistent to close


def make_client(entry: dict):
    """Build the right client from a catalog entry."""
    if entry.get("transport") == "sse":
        return SseMcpClient(entry["url"], headers=entry.get("headers") or {})
    return StdioMcpClient(entry["command"], entry.get("args", []), env=entry.get("env") or {})


# ─── Probe + record ─────────────────────────────────────────────────

def probe_and_validate(entry: dict) -> dict:
    """Connect to a server entry, list tools, validate every schema.
    Returns the validation record for the catalog (never raises)."""
    client = None
    try:
        client = make_client(entry)
        result = client.probe()
    except Exception as e:
        result = {"ok": False, "tools": [], "server_info": {}, "error": f"probe crashed: {e}"}
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass

    record = {
        "probed_at": datetime.now().isoformat(),
        "server_info": result.get("server_info") or {},
    }
    if not result["ok"]:
        record["valid"] = False
        record["problems"] = [result["error"]]
        record["tool_names"] = []
        return record

    v = validate_tools(result["tools"])
    record["valid"] = v["ok"]
    record["problems"] = v["problems"]
    record["tool_names"] = v["tool_names"]
    return record


# ─── Catalog mutations (used by the /mcp slash command) ─────────────

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def parse_add_args(rest: list):
    """Split ['name', 'command...', '--transport', 'stdio'] tail into
    (name, target, transport). The target may contain spaces, so everything
    between the name and the --transport flag is the target."""
    rest = list(rest or [])
    transport = ""
    if "--transport" in rest:
        i = rest.index("--transport")
        if i + 1 < len(rest):
            transport = rest[i + 1]
        rest = rest[:i]
    if len(rest) < 2:
        return "", "", transport
    return rest[0], " ".join(rest[1:]), transport


def add_server(name: str, target: str, transport: str = "") -> dict:
    """Register a server, then PROBE it. Never silently trusts config.
    Returns {"ok": bool, "message": str, "entry": dict}."""
    name = (name or "").strip()
    target = (target or "").strip()
    if not _NAME_RE.match(name):
        return {"ok": False, "message": f"invalid name {name!r} (letters, digits, _ - only, max 32)", "entry": None}
    if not target:
        return {"ok": False, "message": "missing command or url", "entry": None}

    t = (transport or "").strip().lower()
    if t and t not in VALID_TRANSPORTS:
        return {"ok": False, "message": f"transport must be stdio or sse, got {transport!r}", "entry": None}
    if not t:
        # Infer: http(s):// → sse, anything else → stdio
        t = "sse" if re.match(r"^https?://", target) else "stdio"

    if t == "sse":
        if not re.match(r"^https?://", target):
            return {"ok": False, "message": "sse transport needs an http(s) URL", "entry": None}
        entry = {"transport": "sse", "url": target, "headers": {}, "env": {}}
    else:
        parts = shlex.split(target)
        if not parts:
            return {"ok": False, "message": "empty command", "entry": None}
        command = parts[0]
        resolved = shutil.which(command) or (command if Path(command).is_file() else "")
        if not resolved:
            return {"ok": False, "message": f"command not found: {command}", "entry": None}
        entry = {"transport": "stdio", "command": str(Path(resolved)), "args": parts[1:], "env": {}}

    cat = _load_catalog()
    if name in cat["servers"]:
        return {"ok": False, "message": f"server {name!r} already exists (remove it first)", "entry": None}

    _log(f"PROBE add {name} {t} {target}")
    probe = probe_and_validate(entry)
    entry.update({
        "name": name,
        "enabled": bool(probe["valid"]),
        "added": datetime.now().isoformat(),
        "last_validated": probe["probed_at"],
        "valid": probe["valid"],
        "problems": probe["problems"],
        "tool_names": probe["tool_names"],
        "server_info": probe["server_info"],
    })
    cat["servers"][name] = entry
    _save_catalog(cat)

    if probe["valid"]:
        msg = (f"added {name} ({t}, {len(probe['tool_names'])} tools validated) — enabled\n"
               f"  tools: {', '.join(probe['tool_names'])}")
        ok = True
    else:
        msg = (f"registered {name} but LEFT DISABLED — probe failed:\n"
               + "\n".join(f"  ✗ {p}" for p in probe["problems"][:6])
               + f"\n  fix it, then: mcp enable {name}  (or: mcp remove {name})")
        ok = False
    _log(f"ADD {name} valid={probe['valid']}")
    return {"ok": ok, "message": msg, "entry": entry}


def remove_server(name: str) -> dict:
    cat = _load_catalog()
    if name not in cat["servers"]:
        return {"ok": False, "message": f"unknown server: {name}"}
    del cat["servers"][name]
    _save_catalog(cat)
    return {"ok": True, "message": f"removed {name}"}


def set_enabled(name: str, enabled: bool) -> dict:
    """Enable/disable. Enabling RE-PROBES first — a broken server cannot
    be enabled (config is never trusted blindly)."""
    cat = _load_catalog()
    entry = cat["servers"].get(name)
    if not entry:
        return {"ok": False, "message": f"unknown server: {name}"}
    if not enabled:
        entry["enabled"] = False
        _save_catalog(cat)
        return {"ok": True, "message": f"disabled {name}"}

    _log(f"PROBE enable {name}")
    probe = probe_and_validate(entry)
    entry["last_validated"] = probe["probed_at"]
    entry["valid"] = probe["valid"]
    entry["problems"] = probe["problems"]
    entry["tool_names"] = probe["tool_names"]
    entry["server_info"] = probe["server_info"]
    if not probe["valid"]:
        entry["enabled"] = False
        _save_catalog(cat)
        msg = ("REFUSED — probe failed, server stays disabled:\n"
               + "\n".join(f"  ✗ {p}" for p in probe["problems"][:6]))
        _log(f"ENABLE REFUSED {name}")
        return {"ok": False, "message": msg}
    entry["enabled"] = True
    _save_catalog(cat)
    _log(f"ENABLE OK {name} tools={len(probe['tool_names'])}")
    return {"ok": True,
            "message": f"enabled {name} — {len(probe['tool_names'])} tools re-validated: {', '.join(probe['tool_names'])}"}


def revalidate(name: str) -> dict:
    """Re-probe + re-validate without changing enabled state."""
    cat = _load_catalog()
    entry = cat["servers"].get(name)
    if not entry:
        return {"ok": False, "message": f"unknown server: {name}"}
    probe = probe_and_validate(entry)
    entry["last_validated"] = probe["probed_at"]
    entry["valid"] = probe["valid"]
    entry["problems"] = probe["problems"]
    entry["tool_names"] = probe["tool_names"]
    entry["server_info"] = probe["server_info"]
    _save_catalog(cat)
    if probe["valid"]:
        return {"ok": True, "message": f"{name}: valid — {len(probe['tool_names'])} tools: {', '.join(probe['tool_names'])}"}
    return {"ok": False, "message": f"{name}: INVALID — " + "; ".join(probe["problems"][:4])}


# ─── Display (TUI-facing, master_ai.py color codes passed in) ────────

def format_catalog(G: str, R: str, Y: str, C: str, W: str, D: str, X: str) -> str:
    servers = list_servers()
    if not servers:
        return f"  {D}no MCP servers configured — try: mcp add <name> <command|url>{X}"
    lines = [f"\n  {C}MCP servers ({len(servers)}):{X}"]
    for name, s in sorted(servers.items()):
        if s.get("enabled"):
            state = f"{G}enabled{X}"
        elif s.get("valid") is False:
            state = f"{R}INVALID{X}"
        else:
            state = f"{Y}disabled{X}"
        if s.get("transport") == "sse":
            tgt = f"sse {s.get('url', '')}"
        else:
            tgt = " ".join([s.get("command", "")] + list(s.get("args", [])))
            tgt = f"stdio {tgt}"
        ntools = len(s.get("tool_names") or [])
        tools = f"{W}{ntools}{X} tool(s)" if ntools else f"{D}no tools{X}"
        lines.append(f"    {W}{name:<18}{X} {state:<17} {C}{tgt:<60}{X} {tools}")
        if s.get("valid") is False and s.get("problems"):
            lines.append(f"      {R}✗ {s['problems'][0][:110]}{X}")
    lines.append(f"  {D}usage: mcp add <name> <command|url> [--transport stdio|sse] · mcp remove|enable|disable|validate <name> · mcp tools <name>{X}")
    return "\n".join(lines)


def format_tools(name: str, G: str, R: str, Y: str, C: str, W: str, D: str, X: str) -> str:
    s = get_server(name)
    if not s:
        return f"  {Y}unknown server: {name}{X}"
    if not (s.get("tool_names") or s.get("problems")):
        rec = probe_and_validate(s)
        s["tool_names"] = rec["tool_names"]
        s["problems"] = rec["problems"]
    lines = [f"\n  {C}{name} ({s.get('transport')}):{X}"]
    for t in s.get("tool_names") or []:
        lines.append(f"    {W}·{X} {t}")
    for p in s.get("problems") or []:
        lines.append(f"    {R}✗ {p[:120]}{X}")
    return "\n".join(lines)