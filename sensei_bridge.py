#!/usr/bin/env python3
"""sensei_bridge.py — minimal HTTP backend the Sensei Chrome extension talks to.

Replaces the retired stt_server.py surface with a focused, off-grid bridge:
extension POSTs prompts to /chat, this server routes to local Ollama,
parses BROWSER_* directives out of the reply, and returns them as actions[]
for the side panel to dispatch.

Listens on 127.0.0.1:8080 (what sensei_native_host.py and side_panel.js expect).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


HOST = "127.0.0.1"
PORT = 8080
OLLAMA = "http://127.0.0.1:11434"
DEFAULT_MODEL = os.environ.get("SENSEI_MODEL", "qwen2.5:7b")
VISION_MODEL = os.environ.get("SENSEI_VISION_MODEL", "qwen2.5vl:7b")


TOOL_CATALOG = """

AVAILABLE MCP TOOLS:
You have these tools registered through your MCP server right now - they are real, callable, connected. Use them. Do NOT say "I see..." and hallucinate observations - if you need to know what is on a page, navigate first then read.

- sensei.chat(prompt, session_id?, page_context?, mode?): High-level goal entry. mode = review|auto|plan|quick
- sensei.health(): Returns {ok, model, vision_model}
- browser.navigate(url): Emits BROWSER_NAV directive
- browser.click(selector): Emits BROWSER_CLICK directive
- browser.fill(selector, value): Emits BROWSER_FILL directive
- browser.read_local(path, max_bytes?): Reads a file under $HOME (safety-fenced)

When the user gives you a browser-automation goal, plan the BROWSER_* directives and emit them in your output. Your Chrome extension executes them. You are an agent with real tools, not a narrator.
"""

EXT_TOKEN_PATH = os.path.expanduser("~/.master_ai_extension_token")
AUDIT_PATH = os.path.expanduser("~/.sensei_bridge_audit.jsonl")
SAFE_ROOT = os.path.expanduser("~")
SAFE_DENY = ("/.ssh/", "/.gnupg/", "/.master_ai_keys", "/.master_ai_extension_token", "/.aws/")


SYSTEM_PROMPT = """You are a browser automation agent. You control Chrome by emitting directives. You NEVER describe what you did. You NEVER summarize. You NEVER respond with prose. Every reply must be one or more directives from the list below.

DIRECTIVES (emit exactly, one per line, no markdown, no explanation):
BROWSER_NAV: <absolute-url>
BROWSER_CLICK: <css-selector>
BROWSER_FILL: <css-selector> :: <value>
BROWSER_READ
BROWSER_SUBMIT: <form-css-selector>
BROWSER_CLOSE_TAB
ASK: <one-line question>     (use this when you need information the user has not provided)
DONE: <one-line summary of what was accomplished>

HARD RULES:
1. DONE: is ONLY allowed as the very last line, AFTER real browser directives have executed. NEVER as the first or only line.
2. If the user message already contains a [PAGE_CONTEXT] block, DO NOT emit BROWSER_READ — you already have the page. Use the context to pick selectors directly.
3. If the user message is conversational ("hi", "hello", "why", "thanks", "what", "test", a single word, or a question with no browser task), emit ONLY: ASK: What would you like me to do on this page?  — nothing else. No BROWSER_READ, no DONE.
4. If you need a value the user did NOT give you (email, password, name, address, phone, card number), emit ASK: and stop. NEVER invent values. NEVER use placeholder examples.
5. Emit directives only. Zero prose. Zero explanation. Zero apology.
6. Use real CSS selectors: prefer id (#login), name ([name="email"]), aria-label ([aria-label="Search"]).

EXAMPLE — user says "go to google and search cats":
BROWSER_NAV: https://www.google.com
BROWSER_FILL: [name="q"] :: cats
BROWSER_SUBMIT: form[role="search"]
DONE: Searched Google for cats

EXAMPLE — user says "sign up for tubi" (no credentials given):
BROWSER_NAV: https://tubitv.com/signup
ASK: What email and password do you want to use for the Tubi account?

EXAMPLE — user says "show me the page":
BROWSER_READ
DONE: Page context read

EXAMPLE — user provides explicit credentials in the prompt like "fill the email field with bob@real.com and password Hunter2":
BROWSER_FILL: [name="email"] :: bob@real.com
BROWSER_FILL: [name="password"] :: Hunter2
DONE: Filled credentials

WRONG (never do this):
DONE: Signed up for Tubi                           <-- no browser actions, jumped to DONE
BROWSER_FILL: [name="email"] :: user@example.com   <-- fabricated credentials user did not give
BROWSER_FILL: [name="email"] :: bob@real.com       <-- copying example values when user said "sign me up" without giving an email is WRONG. Use ASK.
"""


_session_lock = threading.Lock()
_sessions: dict[str, list[dict]] = {}

# /extension/queue — MCP→Chrome live action transport.
# MCP server (or any external client) POSTs structured actions in;
# the side panel polls and pops them; results land back via the existing
# /extension/action_result audit endpoint. In-memory FIFO scoped by session_id.
_queue_lock = threading.Lock()
_action_queue: dict[str, list[dict]] = {}

# Cap per-session queue depth so a runaway producer can't OOM the bridge.
_QUEUE_MAX_PER_SESSION = 200

# Result loop: when the side panel finishes an action, it POSTs to
# /extension/action_result. We index those by action_id so MCP callers can
# GET /extension/result?action_id=... to see Chrome's outcome — closing the
# loop from "I queued an action" to "Chrome did it, here's what happened."
_results_lock = threading.Lock()
_action_results: dict[str, dict] = {}
_RESULTS_MAX = 1000  # LRU-ish cap; oldest by insertion order get evicted

# Server-side recovery map: action_id -> session_id at enqueue time.
# Used by /extension/action_result so the audit log ALWAYS has a populated
# session_id, even when the panel forgets to echo it in the post body
# (e.g., older side_panel.js not yet reloaded). Trimmed LRU-style.
_action_session_lock = threading.Lock()
_action_session_map: dict[str, str] = {}
_ACTION_SESSION_MAX = 2000

# Session observability for MCP session-fallback diagnostics.
_active_panel_session_lock = threading.Lock()
_active_panel_session: str | None = None
_last_queue_pop_lock = threading.Lock()
_last_queue_pop: dict[str, float] = {}


def _audit(record: dict) -> None:
    record = {"ts": time.time(), **record}
    try:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _ollama_chat(model: str, messages: list[dict], timeout: float = 90.0) -> dict:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": "1h",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


_DIRECTIVE_RE = re.compile(
    r"^\s*(BROWSER_NAV|BROWSER_CLICK|BROWSER_FILL|BROWSER_READ|BROWSER_UPLOAD_FILE|BROWSER_SUBMIT|BROWSER_CLOSE_TAB|ASK|DONE)\s*(?::\s*(.*))?$",
    re.IGNORECASE,
)

PLACEHOLDER_VALUES = {
    "user@example.com", "test@example.com", "test@test.com", "example@example.com",
    "your-email@example.com", "youremail@example.com", "name@example.com",
    "password", "password123", "examplepass123", "yourpassword",
    "test", "testpassword", "test123", "password1", "passw0rd",
    "john", "jane", "john doe", "jane doe", "first last", "firstname lastname",
    "123-456-7890", "555-555-5555", "(555) 555-5555", "5551234567",
    "123 main st", "1234 example st",
    "me@foo.com", "sky9!",
    "bob@real.com", "hunter2",
}


def _is_placeholder_value(value: str) -> bool:
    if not value:
        return True
    return value.strip().lower() in PLACEHOLDER_VALUES


def parse_directives(text: str) -> tuple[list[dict], str]:
    """Extract BROWSER_*/DONE directives from a model reply.

    Returns (actions, cleaned_reply). actions is a list of
    {kind, target, value} dicts matching what side_panel.js expects.
    """
    actions: list[dict] = []
    cleaned_lines: list[str] = []
    has_ask = False
    for raw in text.splitlines():
        line = raw.strip().strip("`").strip()
        m = _DIRECTIVE_RE.match(line)
        if not m:
            cleaned_lines.append(raw)
            continue
        kind = m.group(1).upper()
        rest = (m.group(2) or "").strip()
        if kind == "DONE":
            cleaned_lines.append(f"DONE: {rest}")
            continue
        if kind == "ASK":
            cleaned_lines.append(f"ASK: {rest}")
            has_ask = True
            continue
        target, value = rest, ""
        if "::" in rest:
            target, value = (s.strip() for s in rest.split("::", 1))
        action = {"kind": kind, "target": target, "value": value, "status": "ready"}
        actions.append(action)
    placeholder_fills = [a for a in actions if a["kind"] == "BROWSER_FILL" and _is_placeholder_value(a["value"])]
    if placeholder_fills:
        actions = [a for a in actions if a not in placeholder_fills]
        labels = ", ".join(a["target"] for a in placeholder_fills)
        cleaned_lines = [ln for ln in cleaned_lines if not re.match(r"^\s*DONE:", ln, re.IGNORECASE)]
        cleaned_lines.append(f"ASK: I need real values for these fields (model tried to use placeholder data): {labels}")
        has_ask = True
    if has_ask:
        actions = [a for a in actions if not (a["kind"] == "BROWSER_FILL" and not a["value"])]
        actions = [a for a in actions if a["kind"] != "BROWSER_SUBMIT"]
    cleaned = "\n".join(s for s in cleaned_lines if s.strip())
    return actions, cleaned


_CONVERSATIONAL_RE = re.compile(
    r"^(hi|hey|hello|yo|sup|ok|okay|thanks|thank you|why|what|huh|test|test\.|\?+|cool|nice|good|nope|yes|no|wait)\W*$",
    re.IGNORECASE,
)


def _looks_conversational(prompt: str) -> bool:
    p = (prompt or "").strip()
    if not p:
        return False
    if len(p) <= 3:
        return True
    return bool(_CONVERSATIONAL_RE.match(p))


def _build_messages(prompt: str, page_context: dict | None, history: list[dict]) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT + TOOL_CATALOG}]
    msgs.extend(history)
    user_text = prompt or ""
    skip_heavy_context = _looks_conversational(prompt)
    if page_context and not skip_heavy_context:
        ctx_lines = ["[PAGE_CONTEXT]"]
        if page_context.get("url"):
            ctx_lines.append(f"url: {page_context['url']}")
        if page_context.get("title"):
            ctx_lines.append(f"title: {page_context['title']}")
        if page_context.get("ax_snapshot"):
            snap = page_context["ax_snapshot"]
            ctx_lines.append("ax_snapshot:")
            ctx_lines.append(json.dumps(snap, separators=(",", ":"))[:6000])
        elif page_context.get("text"):
            ctx_lines.append("page_text:")
            ctx_lines.append(str(page_context["text"])[:4000])
        user_text = "\n".join(ctx_lines) + "\n\n[USER]\n" + user_text
    elif page_context and skip_heavy_context:
        url = page_context.get("url") or ""
        title = page_context.get("title") or ""
        if url or title:
            user_text = f"[PAGE: {title} — {url}]\n[USER]\n{user_text}"
    msgs.append({"role": "user", "content": user_text})
    return msgs


def _select_model(body: dict) -> str:
    pc = body.get("page_context") or {}
    if pc.get("screenshot_data_url") or pc.get("image"):
        return VISION_MODEL
    explicit = (body.get("model") or "").strip()
    if explicit:
        return explicit
    return DEFAULT_MODEL


def _safe_local_path(path: str) -> tuple[bool, str]:
    if not path:
        return False, "empty path"
    p = os.path.realpath(os.path.expanduser(path))
    if not p.startswith(SAFE_ROOT):
        return False, "outside home dir"
    for deny in SAFE_DENY:
        if deny in p:
            return False, f"denied: {deny}"
    return True, p


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[bridge] " + (fmt % args) + "\n")

    # ---- response helpers ----
    def _send_json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Master-AI-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._send_json(200, {
                "ok": True,
                "service": "sensei_bridge",
                "model": DEFAULT_MODEL,
                "vision_model": VISION_MODEL,
                "ollama": OLLAMA,
            })
        if self.path == "/version":
            return self._send_json(200, {"version": "0.1.0"})
        if self.path == "/mode":
            claf_env = os.path.expanduser("~/projects/claf/.env")
            claf_mode = "unknown"
            claf_model = os.environ.get("CLAF_LOCAL_MODEL", DEFAULT_MODEL)
            try:
                with open(claf_env) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("CLAF_MODE="):
                            claf_mode = line.split("=", 1)[1].strip()
                        if line.startswith("CLAF_LOCAL_MODEL="):
                            claf_model = line.split("=", 1)[1].strip()
            except Exception:
                pass
            auth = "api_key" if os.environ.get("ANTHROPIC_API_KEY") else "oauth"
            return self._send_json(200, {
                "ok": True,
                "claf_mode": claf_mode,
                "auth": auth,
                "model": claf_model,
            })
        # /extension/queue_state?session_id=... — observe queue depth without
        # consuming. Helps detect session mismatch when an action sits queued
        # forever because Chrome is polling a different session bucket.
        if self.path.startswith("/extension/queue_state"):
            qs = self.path.split("?", 1)
            session_id = None
            if len(qs) == 2:
                for part in qs[1].split("&"):
                    if part.startswith("session_id="):
                        session_id = part.split("=", 1)[1] or None
                        break
            with _queue_lock:
                if session_id:
                    depth = len(_action_queue.get(session_id, []))
                    per_session = {session_id: depth}
                else:
                    per_session = {sid: len(items) for sid, items in _action_queue.items()}
            return self._send_json(200, {"ok": True, "queue_depth": per_session, "session_id": session_id})
        # /extension/queue?session_id=... — pop + return pending actions for the session.
        # Side panel polls this on a short interval to pick up actions that MCP
        # (or any other producer) has pushed in.
        if self.path.startswith("/extension/queue"):
            qs = self.path.split("?", 1)
            session_id = "default"
            if len(qs) == 2:
                for part in qs[1].split("&"):
                    if part.startswith("session_id="):
                        session_id = part.split("=", 1)[1] or "default"
                        break
            with _queue_lock:
                actions = _action_queue.pop(session_id, [])
            with _last_queue_pop_lock:
                _last_queue_pop[session_id] = time.time()
            _audit({"event": "queue_pop", "session_id": session_id, "count": len(actions)})
            return self._send_json(200, {"ok": True, "session_id": session_id, "actions": actions, "count": len(actions)})
        # /extension/sessions — expose known session ids for MCP fallback:
        # primary request id -> mcp-default -> chat-default -> active panel.
        if self.path == "/extension/sessions":
            known = {"mcp-default", "chat-default"}
            with _queue_lock:
                known.update(_action_queue.keys())
                queue_depth = {sid: len(items) for sid, items in _action_queue.items()}
            with _action_session_lock:
                known.update(_action_session_map.values())
            with _active_panel_session_lock:
                active_sid = _active_panel_session
            if active_sid:
                known.add(active_sid)
            with _last_queue_pop_lock:
                last_pop = dict(_last_queue_pop)
            return self._send_json(200, {
                "ok": True,
                "active_side_panel_session": active_sid,
                "known_sessions": sorted(s for s in known if s),
                "queue_depth": queue_depth,
                "last_queue_pop": last_pop,
            })
        # /extension/result?action_id=... — look up Chrome's outcome for a
        # specific queued action. Returns 404 (not yet) or 200 with the result.
        # MCP callers use this to close the loop after pushing an action.
        if self.path.startswith("/extension/result"):
            qs = self.path.split("?", 1)
            action_id = None
            if len(qs) == 2:
                for part in qs[1].split("&"):
                    if part.startswith("action_id="):
                        action_id = part.split("=", 1)[1] or None
                        break
            if not action_id:
                return self._send_json(400, {"error": "action_id required"})
            with _results_lock:
                rec = _action_results.get(action_id)
            if rec is None:
                return self._send_json(200, {"ok": False, "action_id": action_id, "status": "pending"})
            return self._send_json(200, {"ok": True, "action_id": action_id, "result": rec})
        return self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        try:
            if path == "/mode":
                new_mode = str(body.get("mode") or "").strip().lower()
                if new_mode not in ("local", "hybrid", "cloud"):
                    return self._send_json(400, {"error": "mode must be local | hybrid | cloud"})
                import subprocess as _sp
                result = _sp.run(
                    [os.path.expanduser("~/scripts/set_sensei_mode.sh"), new_mode],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    return self._send_json(200, {"ok": True, "claf_mode": new_mode})
                return self._send_json(500, {"ok": False, "error": result.stderr.strip()})
            if path == "/chat":
                return self._handle_chat(body, continuation=False)
            if path == "/chat/continue":
                return self._handle_chat(body, continuation=True)
            if path == "/dispatch":
                # Alias for /extension/queue — MCP server compatibility
                path = "/extension/queue"
            if path == "/extension/queue":
                # MCP / external producer enqueues structured actions.
                # body = {session_id, actions: [{kind, target, value?, ...}], ...}
                session_id = str(body.get("session_id") or "default")
                actions = body.get("actions") or []
                if not isinstance(actions, list):
                    return self._send_json(400, {"error": "actions must be a list"})
                # Stamp each action with a queued_ts and an id if missing.
                stamped = []
                for a in actions:
                    if not isinstance(a, dict) or "kind" not in a:
                        return self._send_json(400, {"error": "each action needs a 'kind'"})
                    a2 = dict(a)
                    a2.setdefault("id", uuid.uuid4().hex)
                    a2["queued_ts"] = time.time()
                    a2["status"] = "queued"
                    # Stamp the origin session onto the action itself so
                    # whichever path echoes the action back in action_result
                    # carries the session through. Belt-and-suspenders with
                    # _action_session_map below.
                    a2["_session_id"] = session_id
                    stamped.append(a2)
                with _queue_lock:
                    bucket = _action_queue.setdefault(session_id, [])
                    overflow = max(0, (len(bucket) + len(stamped)) - _QUEUE_MAX_PER_SESSION)
                    if overflow:
                        # drop oldest queued items, keep the new ones
                        del bucket[:overflow]
                    bucket.extend(stamped)
                    depth = len(bucket)
                # Server-side recovery map — action_id to session_id, used
                # by /extension/action_result if the body lacks session_id.
                with _action_session_lock:
                    for a2 in stamped:
                        _action_session_map[a2["id"]] = session_id
                    if len(_action_session_map) > _ACTION_SESSION_MAX:
                        excess = len(_action_session_map) - _ACTION_SESSION_MAX
                        for old_id in list(_action_session_map.keys())[:excess]:
                            _action_session_map.pop(old_id, None)
                _audit({"event": "queue_push", "session_id": session_id,
                        "count": len(stamped), "depth": depth})
                return self._send_json(200, {
                    "ok": True, "session_id": session_id,
                    "count": len(stamped), "queue_depth": depth,
                    "action_ids": [a["id"] for a in stamped],
                })
            if path == "/extension/action_result":
                # Stash by action_id so MCP callers can poll /extension/result.
                aid = body.get("action_id") or (body.get("action") or {}).get("id")
                # Resolve session_id (operator STEP 2 of SENSEI hardening):
                # body.session_id -> action._session_id -> action.session_id
                # -> server-side map by action_id -> "unknown". Never empty.
                action_obj = body.get("action") or {}
                resolved_sid = (
                    body.get("session_id")
                    or action_obj.get("_session_id")
                    or action_obj.get("session_id")
                )
                if not resolved_sid and aid:
                    with _action_session_lock:
                        resolved_sid = _action_session_map.get(aid)
                if not resolved_sid:
                    resolved_sid = "unknown"
                if aid:
                    record = {
                        "action_id": aid,
                        "session_id": resolved_sid,
                        "verdict": body.get("verdict"),
                        "result": body.get("result"),
                        "final_state": body.get("final_state"),
                        "completed_ts": time.time(),
                    }
                    with _results_lock:
                        _action_results[aid] = record
                        # Trim oldest if over the cap.
                        if len(_action_results) > _RESULTS_MAX:
                            for old_id in list(_action_results.keys())[: len(_action_results) - _RESULTS_MAX]:
                                _action_results.pop(old_id, None)
                # Audit payload: inject resolved session_id, then merge body
                # so body's session_id wins if present. If body had empty/missing
                # session_id, the resolved value sticks.
                audit_payload = {
                    "event": "action_result",
                    "browser_action_result": True,
                    "session_id": resolved_sid,
                }
                audit_payload.update(body)
                if not audit_payload.get("session_id"):
                    audit_payload["session_id"] = resolved_sid
                _audit(audit_payload)
                return self._send_json(200, {
                    "ok": True, "indexed": bool(aid), "session_id": resolved_sid,
                })
            if path == "/extension/approve_action":
                _audit({"event": "approve_action", **body})
                return self._send_json(200, {"ok": True})
            if path == "/extension/classify_domain":
                return self._handle_classify(body)
            if path == "/extension/refusal_audit":
                _audit({"event": "refusal_audit", **body})
                return self._send_json(200, {"ok": True})
            if path == "/extension/resolve_local_file":
                return self._handle_resolve_file(body)
            if path == "/extension/read_local_file":
                return self._handle_read_file(body)
            if path == "/tool/find":
                return self._handle_tool_find(body)
            if path == "/tool/describe_step":
                return self._handle_describe_step(body)
            return self._send_json(404, {"error": f"unknown route {path}"})
        except Exception as e:
            _audit({"event": "handler_error", "path": path, "error": str(e)})
            return self._send_json(500, {"error": str(e), "path": path})

    # ---- /chat ----
    def _handle_chat(self, body: dict, continuation: bool) -> None:
        prompt = str(body.get("prompt") or "")
        session_id = str(body.get("session_id") or f"sensei-{uuid.uuid4()}")
        page_context = body.get("page_context") or {}
        model = _select_model(body)

        with _active_panel_session_lock:
            global _active_panel_session
            _active_panel_session = session_id

        with _session_lock:
            history = list(_sessions.get(session_id, []))
        msgs = _build_messages(prompt, page_context, history)

        t0 = time.time()
        try:
            resp = _ollama_chat(model, msgs, timeout=120.0)
        except urllib.error.URLError as e:
            return self._send_json(503, {
                "error": f"ollama unreachable: {e}",
                "reply": "[bridge] could not reach local model.",
                "actions": [],
                "session_id": session_id,
            })
        elapsed = round(time.time() - t0, 2)

        reply_text = (resp.get("message") or {}).get("content") or ""
        actions, cleaned = parse_directives(reply_text)
        turn_id = uuid.uuid4().hex

        with _session_lock:
            new_hist = history + [
                {"role": "user", "content": prompt or "(continue)"},
                {"role": "assistant", "content": reply_text},
            ]
            _sessions[session_id] = new_hist[-30:]

        _audit({
            "event": "chat",
            "continuation": continuation,
            "session_id": session_id,
            "turn_id": turn_id,
            "model": model,
            "elapsed_s": elapsed,
            "actions_count": len(actions),
            "prompt": prompt[:200],
        })

        return self._send_json(200, {
            "reply": cleaned or reply_text,
            "actions": actions,
            "blocked_actions": [],
            "session_id": session_id,
            "turn_id": turn_id,
            "model": model,
            "elapsed_s": elapsed,
        })

    # ---- /extension/classify_domain ----
    def _handle_classify(self, body: dict) -> None:
        url = str(body.get("url") or "")
        host = ""
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ""
        except Exception:
            pass
        category = "unknown"
        low = host.lower()
        if any(k in low for k in ("indeed.", "ziprecruiter.", "linkedin.", "lever.", "greenhouse.")):
            category = "job_board"
        elif any(k in low for k in ("docs.google.", "drive.google.", "sheets.google.")):
            category = "drive"
        elif any(k in low for k in ("mail.google.", "outlook.", "yahoo.com/mail")):
            category = "email"
        return self._send_json(200, {"ok": True, "host": host, "category": category})

    # ---- /extension/resolve_local_file ----
    def _handle_resolve_file(self, body: dict) -> None:
        hint = str(body.get("hint") or body.get("path") or "")
        ok, resolved = _safe_local_path(hint)
        if not ok:
            return self._send_json(200, {"ok": False, "error": resolved})
        if not os.path.exists(resolved):
            return self._send_json(200, {"ok": False, "error": f"not found: {resolved}"})
        return self._send_json(200, {
            "ok": True,
            "path": resolved,
            "size": os.path.getsize(resolved),
            "is_file": os.path.isfile(resolved),
        })

    # ---- /extension/read_local_file ----
    def _handle_read_file(self, body: dict) -> None:
        path = str(body.get("path") or "")
        ok, resolved = _safe_local_path(path)
        if not ok:
            return self._send_json(200, {"ok": False, "error": resolved})
        if not os.path.isfile(resolved):
            return self._send_json(200, {"ok": False, "error": "not a file"})
        max_bytes = int(body.get("max_bytes") or 65536)
        try:
            with open(resolved, "rb") as f:
                data = f.read(max_bytes)
            return self._send_json(200, {
                "ok": True,
                "path": resolved,
                "bytes": len(data),
                "text": data.decode("utf-8", errors="replace"),
            })
        except Exception as e:
            return self._send_json(200, {"ok": False, "error": str(e)})

    # ---- /tool/find ----
    def _handle_tool_find(self, body: dict) -> None:
        query = str(body.get("query") or "")
        results: list[dict] = []
        # Simple bounded find under ~/scripts and ~/Desktop
        roots = [os.path.expanduser("~/scripts"), os.path.expanduser("~/Desktop")]
        for root in roots:
            for dirpath, _, filenames in os.walk(root):
                for name in filenames:
                    if query.lower() in name.lower():
                        results.append({"path": os.path.join(dirpath, name)})
                        if len(results) >= 50:
                            break
                if len(results) >= 50:
                    break
        return self._send_json(200, {"ok": True, "results": results})

    # ---- /tool/describe_step ----
    def _handle_describe_step(self, body: dict) -> None:
        return self._send_json(200, {
            "ok": True,
            "summary": str(body.get("step") or "(no step)")[:200],
        })


def serve() -> int:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"[bridge] listening on http://{HOST}:{PORT}\n")
    sys.stderr.write(f"[bridge] default_model={DEFAULT_MODEL} vision_model={VISION_MODEL}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
