#!/usr/bin/env python3
"""agui_shim.py — AG-UI (CopilotKit) runtime shim in front of sensei_bridge.py.

Exposes the minimal AG-UI agent surface:
  GET  /info
  POST /agent/default/run  -> SSE stream of RunStarted/TextMessage/ToolCall/RunFinished
  POST /extension/action_result  -> transparent proxy to bridge /extension/action_result

Translation rules:
  - RunAgentInput.messages -> bridge /chat prompt (last user message).
  - RunAgentInput.context[] -> bridge page_context dict.
  - bridge /chat reply -> TextMessage event.
  - bridge actions[] -> ToolCall events.
  - ASK or sensitive actions -> ToolCall with interrupt=true + Interrupt event.
  - X-Master-AI-Token and CORS headers are forwarded/echoed.

Dependencies: stdlib + Flask.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

from flask import Flask, Response, jsonify, request

APP = Flask(__name__)

BRIDGE_BASE = os.environ.get("SENSEI_BRIDGE_URL", "http://127.0.0.1:8791")
EXT_TOKEN_PATH = os.path.expanduser("~/.master_ai_extension_token")


def _load_token() -> str | None:
    try:
        with open(EXT_TOKEN_PATH) as f:
            return f.read().strip() or None
    except Exception:
        return None


_EXT_TOKEN = _load_token()


@APP.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Master-AI-Token"
    resp.headers["Access-Control-Expose-Headers"] = "X-Master-AI-Token"
    return resp


@APP.route("/info", methods=["GET", "OPTIONS"])
def info():
    if request.method == "OPTIONS":
        return "", 204
    return jsonify(
        {
            "agentName": "default",
            "agentDescription": "Sensei browser agent (CopilotKit shim)",
            "version": "0.1.0",
            "supportsInterrupts": True,
        }
    )


def _require_auth() -> tuple[dict, int] | None:
    if not _EXT_TOKEN:
        return None
    hdr = request.headers.get("X-Master-AI-Token", "")
    if hdr != _EXT_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    return None


def _bridge_headers() -> dict[str, str]:
    hdrs = {"Content-Type": "application/json"}
    token = request.headers.get("X-Master-AI-Token") or _EXT_TOKEN
    if token:
        hdrs["X-Master-AI-Token"] = token
    return hdrs


def _bridge_post(path: str, body: dict, timeout: float = 120.0) -> dict:
    url = f"{BRIDGE_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=_bridge_headers(), method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _page_context_from_context(ctx: list[Any]) -> dict:
    """Flatten AG-UI context items into a single bridge page_context dict."""
    merged: dict = {}
    for item in ctx or []:
        if isinstance(item, dict):
            if "page_context" in item and isinstance(item["page_context"], dict):
                merged.update(item["page_context"])
            else:
                merged.update(item)
    return merged


_HUMAN_GATE_WORDS = [
    "payment",
    "purchase",
    "checkout",
    "credit card",
    "ssn",
    "social security",
    "submit application",
    "delete",
    "password",
]


def _is_human_gate(action: dict, reply: str) -> bool:
    if action.get("kind") == "ASK":
        return True
    text = (reply or "").lower()
    return any(w in text for w in _HUMAN_GATE_WORDS)


def _event(kind: str, payload: dict) -> str:
    payload["type"] = kind
    return f"event: {kind.lower()}\ndata: {json.dumps(payload)}\n\n"


@APP.route("/agent/default/run", methods=["POST", "OPTIONS"])
def run_agent():
    if request.method == "OPTIONS":
        return "", 204
    err = _require_auth()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    messages = body.get("messages") or []
    thread_id = body.get("threadId") or str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    prompt = ""
    for m in reversed(messages):
        if (m or {}).get("role") == "user":
            prompt = str(m.get("content") or "")
            break
    if not prompt:
        return jsonify({"error": "no user message found"}), 400

    ctx = body.get("context") or []
    page_context = _page_context_from_context(ctx)

    run_cfg = body.get("runConfig") or {}
    mode = str(run_cfg.get("mode") or body.get("mode") or "auto").lower()
    review = mode == "review"

    session_id = thread_id
    # Capture request state before entering the streaming generator, which may
    # execute outside the Flask request context.
    caller_token = request.headers.get("X-Master-AI-Token") or _EXT_TOKEN

    def _bridge_headers_local() -> dict[str, str]:
        hdrs = {"Content-Type": "application/json"}
        if caller_token:
            hdrs["X-Master-AI-Token"] = caller_token
        return hdrs

    def _bridge_post_local(path: str, body: dict, timeout: float = 120.0) -> dict:
        url = f"{BRIDGE_BASE}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=_bridge_headers_local(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))

    def _stream():
        yield _event("RunStarted", {"threadId": thread_id, "runId": run_id})

        try:
            chat_body = {
                "prompt": prompt,
                "session_id": session_id,
                "page_context": page_context,
            }
            result = _bridge_post_local("/chat", chat_body)
        except urllib.error.HTTPError as e:
            err_text = e.read().decode("utf-8", errors="replace")[:500]
            yield _event("TextMessage", {"content": f"[shim] bridge error: {err_text}"})
            yield _event("RunFinished", {"threadId": thread_id, "status": "error"})
            return
        except Exception as e:
            yield _event("TextMessage", {"content": f"[shim] bridge unreachable: {e}"})
            yield _event("RunFinished", {"threadId": thread_id, "status": "error"})
            return

        reply = str(result.get("reply") or result.get("content") or "").strip()
        if reply:
            yield _event("TextMessage", {"content": reply})

        actions = result.get("actions") or []
        for action in actions:
            aid = action.get("id") or uuid.uuid4().hex
            name = action.get("kind", "tool")
            args = {
                "target": action.get("target", ""),
                "value": action.get("value", ""),
                "status": action.get("status", "ready"),
            }
            interrupt = review or _is_human_gate(action, reply)
            yield _event(
                "ToolCall",
                {
                    "toolCallId": aid,
                    "name": name,
                    "args": args,
                    "interrupt": interrupt,
                },
            )
            if interrupt:
                yield _event(
                    "Interrupt",
                    {
                        "threadId": thread_id,
                        "toolCallId": aid,
                        "reason": "review" if review else "human-gate",
                    },
                )

        yield _event(
            "RunFinished",
            {
                "threadId": thread_id,
                "runId": run_id,
                "status": "done",
                "session_id": result.get("session_id", session_id),
                "turn_id": result.get("turn_id"),
                "model": result.get("model"),
                "elapsed_s": result.get("elapsed_s"),
                "interrupt": any((review or _is_human_gate(a, reply)) for a in actions),
            },
        )

    return Response(_stream(), mimetype="text/event-stream")


@APP.route("/extension/action_result", methods=["POST", "OPTIONS"])
def action_result_proxy():
    if request.method == "OPTIONS":
        return "", 204
    err = _require_auth()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        result = _bridge_post("/extension/action_result", body)
        return jsonify(result)
    except urllib.error.HTTPError as e:
        return jsonify({"error": e.read().decode("utf-8", errors="replace")}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 503


def main() -> int:
    port = int(os.environ.get("AGUI_SHIM_PORT", "5001"))
    host = os.environ.get("AGUI_SHIM_HOST", "127.0.0.1")
    sys.stderr.write(f"[agui_shim] listening on http://{host}:{port}\n")
    sys.stderr.write(
        f"[agui_shim] bridge={BRIDGE_BASE} token={'set' if _EXT_TOKEN else 'not required'}\n"
    )
    APP.run(host=host, port=port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
