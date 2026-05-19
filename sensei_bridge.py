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
EXT_TOKEN_PATH = os.path.expanduser("~/.master_ai_extension_token")
AUDIT_PATH = os.path.expanduser("~/.sensei_bridge_audit.jsonl")
SAFE_ROOT = os.path.expanduser("~")
SAFE_DENY = ("/.ssh/", "/.gnupg/", "/.master_ai_keys", "/.master_ai_extension_token", "/.aws/")


SYSTEM_PROMPT = """You drive a Chrome browser through Sensei, the user's local extension.
To act, emit BROWSER_* directives on their own lines, exactly as shown.

Grammar (one per line, no markdown fences, no JSON):
  BROWSER_NAV: <absolute-url>
  BROWSER_CLICK: <css-selector>
  BROWSER_FILL: <css-selector> :: <value>
  BROWSER_READ
  BROWSER_UPLOAD: <css-selector> :: <absolute-file-path>
  BROWSER_SUBMIT: <form-css-selector>
  BROWSER_CLOSE
  DONE: <one-line summary>

Rules:
  - If the page is unknown, emit BROWSER_READ first to get the page context.
  - Use real CSS selectors (id, name, aria-label, data-testid). Prefer #id or [name="x"].
  - One directive per line. No prose between directives.
  - When the user goal is satisfied, emit DONE: <summary> and stop.
  - Do NOT explain. Do NOT apologize. Emit directives only.
"""


_session_lock = threading.Lock()
_sessions: dict[str, list[dict]] = {}


def _audit(record: dict) -> None:
    record = {"ts": time.time(), **record}
    try:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _ollama_chat(model: str, messages: list[dict], timeout: float = 90.0) -> dict:
    body = json.dumps({"model": model, "messages": messages, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


_DIRECTIVE_RE = re.compile(
    r"^\s*(BROWSER_NAV|BROWSER_CLICK|BROWSER_FILL|BROWSER_READ|BROWSER_UPLOAD|BROWSER_SUBMIT|BROWSER_CLOSE|DONE)\s*(?::\s*(.*))?$",
    re.IGNORECASE,
)


def parse_directives(text: str) -> tuple[list[dict], str]:
    """Extract BROWSER_*/DONE directives from a model reply.

    Returns (actions, cleaned_reply). actions is a list of
    {kind, target, value} dicts matching what side_panel.js expects.
    """
    actions: list[dict] = []
    cleaned_lines: list[str] = []
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
        target, value = rest, ""
        if "::" in rest:
            target, value = (s.strip() for s in rest.split("::", 1))
        action = {"kind": kind, "target": target, "value": value, "status": "ready"}
        actions.append(action)
    cleaned = "\n".join(s for s in cleaned_lines if s.strip())
    return actions, cleaned


def _build_messages(prompt: str, page_context: dict | None, history: list[dict]) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs.extend(history)
    user_text = prompt or ""
    if page_context:
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
        return self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        try:
            if path == "/chat":
                return self._handle_chat(body, continuation=False)
            if path == "/chat/continue":
                return self._handle_chat(body, continuation=True)
            if path == "/extension/action_result":
                _audit({"event": "action_result", **body})
                return self._send_json(200, {"ok": True})
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
