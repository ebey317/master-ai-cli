#!/usr/bin/env python3
"""Native messaging host for the Sensei Chrome extension.

Protocol:
    {"type":"ping","id":"..."} -> {"type":"pong","id":"...","ok":true}
    {"type":"tool_request","id":"...","token":"...","payload":{...}}
      -> {"type":"tool_response","id":"...","ok":true|false,...}

The host is intentionally a narrow shim. It never evals payloads and only
forwards allowlisted local HTTP endpoints to stt_server.py with the shared
extension token.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import urllib.error
import urllib.request


TOKEN_PATH = os.path.expanduser("~/.master_ai_extension_token")
DEFAULT_BACKEND = "http://127.0.0.1:8791"  # 2026-09-02: matches the :8080->:8791 migration (2026-08-21)
ALLOWED_ENDPOINTS = {
    "/health",
    "/chat",
    "/chat/continue",
    "/extension/approve_action",
    "/extension/read_local_file",
    "/tool/find",
    "/tool/describe_step",
}


def _read_token() -> str:
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _read_message():
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len:
        return None
    if len(raw_len) != 4:
        raise ValueError("truncated native-message length header")
    length = struct.unpack("<I", raw_len)[0]
    if length <= 0 or length > 1024 * 1024:
        raise ValueError("native-message length outside allowed range")
    raw = sys.stdin.buffer.read(length)
    if len(raw) != length:
        raise ValueError("truncated native-message body")
    msg = json.loads(raw.decode("utf-8"))
    if not isinstance(msg, dict):
        raise ValueError("native-message body must be an object")
    return msg


def _write_message(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(raw)))
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _reject(msg, error, code="rejected"):
    return {
        "type": "tool_response",
        "id": msg.get("id"),
        "ok": False,
        "error": error,
        "error_code": code,
    }


def _validate_token(msg) -> tuple[bool, str]:
    expected = _read_token()
    supplied = str(msg.get("token") or (msg.get("payload") or {}).get("token") or "")
    if not expected:
        return False, "extension token file missing"
    if supplied != expected:
        return False, "missing or invalid extension token"
    return True, ""


def _forward(msg):
    ok, reason = _validate_token(msg)
    if not ok:
        return _reject(msg, reason, "auth_failed")
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return _reject(msg, "payload must be an object", "bad_payload")
    if "eval" in payload or "code" in payload and payload.get("endpoint") not in {"/chat", "/chat/continue"}:
        return _reject(msg, "eval-style native payloads are refused", "eval_refused")
    endpoint = str(payload.get("endpoint") or "/health")
    if endpoint not in ALLOWED_ENDPOINTS:
        return _reject(msg, f"endpoint not allowed: {endpoint}", "endpoint_refused")
    method = str(payload.get("method") or ("GET" if endpoint == "/health" else "POST")).upper()
    if method not in {"GET", "POST"}:
        return _reject(msg, "method must be GET or POST", "method_refused")
    backend = str(payload.get("backend_url") or DEFAULT_BACKEND).rstrip("/") or DEFAULT_BACKEND
    body = payload.get("body")
    data = None
    headers = {"X-Master-AI-Token": _read_token()}
    if method == "POST":
        headers["Content-Type"] = "application/json"
        data = json.dumps(body if isinstance(body, dict) else {}).encode("utf-8")
    req = urllib.request.Request(
        backend + endpoint,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            text = res.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = {"text": text}
            return {
                "type": "tool_response",
                "id": msg.get("id"),
                "ok": 200 <= int(res.status) < 300,
                "status": int(res.status),
                "body": parsed,
            }
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"text": text}
        return {
            "type": "tool_response",
            "id": msg.get("id"),
            "ok": False,
            "status": int(e.code),
            "body": parsed,
        }
    except Exception as e:
        return _reject(msg, str(e), "forward_failed")


def handle_message(msg):
    typ = str(msg.get("type") or "")
    if typ == "ping":
        return {"type": "pong", "id": msg.get("id"), "ok": True}
    if typ == "tool_request":
        return _forward(msg)
    return _reject(msg, f"unknown message type: {typ}", "unknown_type")


def main() -> int:
    while True:
        try:
            msg = _read_message()
            if msg is None:
                break
            _write_message(handle_message(msg))
        except Exception as e:
            _write_message({"type": "tool_response", "ok": False, "error": str(e)})
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
