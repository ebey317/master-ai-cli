"""Eval v2 driver — drives the LIVE Master AI /chat lane, one task per attempt.

Contract (verified live 2026-10-15, after the create_content/edit_old fixes):

  * POST /chat with source=chrome_extension + mode=auto -> the server parses
    directives, dispatches terminal actions SERVER-SIDE (RUN/CREATE/READ/EDIT),
    loops internally up to round_budget, and returns the final reply.
  * One POST == one full task attempt. Success is judged by execution-verified
    end state (the task's check()), never by the model's claims.

Stdlib only. Token from ~/.master_ai_extension_token (same as stt_server).
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8080"
_TIMEOUT_S = 420  # server-side loop may take several cloud rounds


def _token() -> str:
    return (Path.home() / ".master_ai_extension_token").read_text().strip()


def post_chat(prompt: str, *, session_id: str, round_budget: int = 10) -> dict:
    """One task attempt through the real /chat lane. Returns the server JSON."""
    body = json.dumps(
        {
            "prompt": prompt,
            "mode": "auto",
            "source": "chrome_extension",
            "session_id": session_id,
            "round_budget": round_budget,
        }
    ).encode()
    req = urllib.request.Request(
        BASE + "/chat",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Master-AI-Token": _token(),
        },
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode())
    payload["_latency_s"] = round(time.time() - t0, 2)
    return payload


def post_continue(
    parent_turn_id: str, *, session_id: str, action_results: list | None = None
) -> dict:
    """M9 continuation round — what the real extension sends when done=false."""
    body = json.dumps(
        {
            "parent_turn_id": parent_turn_id,
            "source": "chrome_extension",
            "mode": "auto",
            "session_id": session_id,
            "action_results": action_results if action_results is not None else [],
        }
    ).encode()
    req = urllib.request.Request(
        BASE + "/chat/continue",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Master-AI-Token": _token(),
        },
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode())
    payload["_latency_s"] = round(time.time() - t0, 2)
    return payload


def _continuation_results(payload: dict) -> list[dict]:
    """Report the previous round's dispatched outcomes, extension-style.

    2026-10-15: sending [] made the server's continuation template read as
    "no new action dispatched", which nudged the model into premature
    closure ("the task was already completed") -- found live via Eval v2 on
    bugfix_script. Outcome is derived from evidence in the reply's
    server-dispatched section; defaults to success for dispatched kinds
    (the server ran them and surfaced blockers in the reply text).
    """
    reply = payload.get("reply") or ""
    rows = []
    for action in payload.get("actions") or []:
        kind = (action.get("kind") or "").upper()
        target = action.get("target") or ""
        result = "success"
        if kind in ("EDIT", "CREATE"):
            marker = f"[{kind} {target}]"
            if marker in reply:
                seg = reply.split(marker, 1)[1][:48].lower()
                if "blocked" in seg or "refused" in seg:
                    result = "failure"
        rows.append(
            {
                "action_id": action.get("id"),
                "kind": kind,
                "target": target,
                "verdict": "accept",
                "result": result,
                "final_state": {"ok": result == "success"},
            }
        )
    return rows


def drive_task(
    prompt: str, *, session_id: str, round_budget: int = 10, max_continuations: int = 4
) -> dict:
    """Full task attempt: /chat plus /chat/continue until done or budget out.

    2026-10-15: earlier the driver stopped after one POST, so any task whose
    completion needed a second round (done=false, rounds remaining) was
    scored as failed even though the real client (extension) continues via
    /chat/continue. This mirrors the extension smoke test's loop, including
    reporting the previous round's action outcomes.
    """
    payload = post_chat(prompt, session_id=session_id, round_budget=round_budget)
    turns = 1
    total_latency = payload.get("_latency_s", 0.0)
    while not payload.get("done") and turns <= max_continuations:
        parent = payload.get("turn_id")
        remaining = payload.get("round_remaining")
        if not parent or (isinstance(remaining, int) and remaining <= 0):
            break
        payload = post_continue(
            parent,
            session_id=session_id,
            action_results=_continuation_results(payload),
        )
        turns += 1
        total_latency += payload.get("_latency_s", 0.0)
    return {
        "reply": payload.get("reply") or "",
        "done": bool(payload.get("done")),
        "turns": turns,
        "route": payload.get("route"),
        "model": payload.get("model"),
        "latency_s": round(total_latency, 2),
    }


def probe_health() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False
