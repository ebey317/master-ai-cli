"""
browser_bridge.py — Directive executor for the apply-job-session skill.

Maps BROWSER_* directive strings (as emitted by recipe.py adapter phases)
to real HTTP calls on the sensei bridge at http://127.0.0.1:8080.

BROWSER_SUBMIT is NEVER executed automatically — it always raises
BridgeSubmitRefused. That is the primary safety guarantee of this module.

Usage:
    from browser_bridge import execute_directives, BridgeSubmitRefused

    results = execute_directives([
        "BROWSER_NAV: https://www.indeed.com/viewjob?jk=abc123",
        "BROWSER_WAIT: 3000",
        "BROWSER_READ_PAGE: main",
    ])
    # returns list of {directive, outcome, result, error}
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import urllib.error
from typing import Optional

BRIDGE = "http://127.0.0.1:8080"
SESSION = "mcp-default"
WAIT_SECONDS = 25      # max seconds to poll for a bridge result
POLL_MS = 300


class BridgeError(Exception):
    """Bridge unreachable or returned an error."""


class BridgeSubmitRefused(Exception):
    """BROWSER_SUBMIT was requested — always refused, operator must approve."""


# ─── Low-level bridge plumbing ───────────────────────────────────────

def _http(method: str, path: str, body: dict = None, timeout: float = 5.0) -> dict:
    url = BRIDGE + path
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.reason}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _push(kind: str, payload: dict) -> Optional[str]:
    """Push one action to the bridge. Returns action_id or None on failure."""
    action = {"kind": kind, **payload}
    body = {"session_id": SESSION, "actions": [action]}
    resp = _http("POST", "/extension/queue", body=body, timeout=3.0)
    if not resp.get("ok"):
        return None
    j = resp.get("json") or {}
    aid = j.get("action_id")
    if isinstance(aid, str) and aid:
        return aid
    aids = j.get("action_ids")
    if isinstance(aids, list) and aids:
        return aids[0]
    return None


def _await(action_id: str, wait: float = WAIT_SECONDS) -> dict:
    """Poll until result is ready. Returns result dict."""
    deadline = time.time() + wait
    while time.time() < deadline:
        r = _http("GET", f"/extension/result?session_id={SESSION}&action_id={action_id}", timeout=2.0)
        if r.get("ok") and r.get("json"):
            j = r["json"]
            if j.get("ok") and j.get("result") is not None:
                return j
        time.sleep(POLL_MS / 1000.0)
    return {"ok": False, "reason": "timeout", "action_id": action_id}


def _dispatch(kind: str, payload: dict) -> dict:
    """Push + await. Returns bridge result dict."""
    aid = _push(kind, payload)
    if not aid:
        return {"ok": False, "reason": "push_failed"}
    return _await(aid)


# ─── Directive parsers ───────────────────────────────────────────────

def _parse_directive(raw: str) -> tuple[str, str]:
    """Split 'BROWSER_FOO: payload' → ('BROWSER_FOO', 'payload').
    Returns ('UNKNOWN', raw) if parsing fails."""
    raw = raw.strip()
    m = re.match(r"^(BROWSER_[A-Z_]+)\s*:\s*(.*)", raw, re.DOTALL)
    if m:
        return m.group(1), m.group(2).strip()
    return "UNKNOWN", raw


# ─── Per-directive handlers ──────────────────────────────────────────

def _exec_nav(payload: str) -> dict:
    """BROWSER_NAV: <url>"""
    url = payload.strip()
    if not url.startswith("http"):
        url = "https://" + url
    result = _dispatch("BROWSER_NAV", {"target": url})
    final = (result.get("result") or {}).get("final_state") or {}
    return {
        "outcome": "ok" if result.get("ok") else "error",
        "url": final.get("url", url),
        "title": final.get("title", ""),
        "raw": result,
    }


def _exec_wait(payload: str) -> dict:
    """BROWSER_WAIT: <milliseconds>"""
    try:
        ms = int(re.sub(r"\D", "", payload) or "1000")
    except ValueError:
        ms = 1000
    ms = min(ms, 10_000)  # cap at 10 seconds
    time.sleep(ms / 1000.0)
    return {"outcome": "ok", "slept_ms": ms}


def _exec_read_page(payload: str) -> dict:
    """BROWSER_READ_PAGE: <section-hint>
    Reads the page accessibility tree. Returns full text (no truncation).
    payload is a hint ('main', 'form', etc.) — currently passed as a label
    hint; the extension returns the full tree regardless."""
    result = _dispatch("BROWSER_READ_PAGE", {})
    if not result.get("ok"):
        return {"outcome": "error", "error": result.get("reason", "bridge error"), "text": ""}

    # The bridge result → result.result.final_state.page_context
    inner = result.get("result") or {}
    final = inner.get("final_state") or {}
    page_ctx = final.get("page_context") or {}

    # Extract useful fields
    url = page_ctx.get("url", "")
    title = page_ctx.get("title", "")
    focused = page_ctx.get("focused_text", "")
    interactive = page_ctx.get("interactive_elements", "")
    dom_state = page_ctx.get("dom_state") or {}

    # Compose a rich text blob for the recipe to parse
    parts = []
    if title:
        parts.append(f"PAGE_TITLE: {title}")
    if url:
        parts.append(f"PAGE_URL: {url}")
    if focused:
        parts.append(f"FOCUSED_TEXT:\n{focused}")
    if interactive:
        parts.append(f"INTERACTIVE_ELEMENTS:\n{interactive}")
    if dom_state:
        parts.append(f"DOM_STATE:\n{json.dumps(dom_state, indent=2)[:4000]}")

    text = "\n\n".join(parts)
    return {
        "outcome": "ok",
        "text": text,
        "url": url,
        "title": title,
        "dom_state": dom_state,
        "raw": result,
    }


def _exec_click(payload: str) -> dict:
    """BROWSER_CLICK: <selector-or-label>
    payload may be a CSS selector (#id, .class, input[name=...]) or a
    visible label string. Bridge uses BROWSER_CLICK which is label-aware."""
    target = payload.strip()
    result = _dispatch("BROWSER_CLICK", {"target": target})
    final = (result.get("result") or {}).get("final_state") or {}
    ok = result.get("ok") and final.get("result") != "failure"
    return {
        "outcome": "ok" if ok else "error",
        "clicked": final.get("clicked", target),
        "error": final.get("error") if not ok else None,
        "raw": result,
    }


def _exec_fill(payload: str) -> dict:
    """BROWSER_FILL: <selector> :: <value>
    Splits on ' :: ' to get target and value."""
    if " :: " in payload:
        target, value = payload.split(" :: ", 1)
    else:
        return {"outcome": "error", "error": "BROWSER_FILL missing ' :: ' separator"}
    target = target.strip()
    value = value.strip()
    result = _dispatch("BROWSER_FILL", {"target": target, "value": value})
    final = (result.get("result") or {}).get("final_state") or {}
    ok = result.get("ok") and final.get("result") != "failure"
    return {
        "outcome": "ok" if ok else "error",
        "target": target,
        "value_len": len(value),
        "error": final.get("reason") if not ok else None,
        "raw": result,
    }


def _exec_find(payload: str) -> dict:
    """BROWSER_FIND: <label>
    Reads the page and looks for the named element in the accessibility tree.
    Returns the first selector that looks like a match."""
    label = payload.strip()
    read_result = _exec_read_page("find")
    text = read_result.get("text", "")
    # Look for the label in interactive elements
    found = label.lower() in text.lower()
    return {
        "outcome": "ok" if found else "not_found",
        "label": label,
        "found": found,
        "page_text_excerpt": text[:500],
    }


def _exec_submit(payload: str) -> dict:
    """BROWSER_SUBMIT: ALWAYS REFUSED. Operator must approve before submit."""
    raise BridgeSubmitRefused(
        f"BROWSER_SUBMIT was requested (target: {payload!r}) — "
        "refused by browser_bridge safety gate. "
        "Operator must review the filled form and explicitly trigger submission."
    )


def _exec_upload_file(payload: str) -> dict:
    """BROWSER_UPLOAD_FILE: <selector> :: <file-path>
    Uses Chrome DevTools Protocol (CDP) file-input bridge in the extension."""
    if " :: " in payload:
        selector, file_path = payload.split(" :: ", 1)
    else:
        return {"outcome": "error", "error": "BROWSER_UPLOAD_FILE missing ' :: ' separator"}
    selector = selector.strip()
    file_path = file_path.strip()
    # The extension's CDP DOM.setFileInputFiles bridge handles this.
    result = _dispatch("BROWSER_UPLOAD_FILE", {"selector": selector, "file_path": file_path})
    ok = result.get("ok")
    return {
        "outcome": "ok" if ok else "error",
        "selector": selector,
        "file_path": file_path,
        "error": result.get("reason") if not ok else None,
        "raw": result,
    }


# ─── Directive dispatcher ────────────────────────────────────────────

_HANDLERS = {
    "BROWSER_NAV": _exec_nav,
    "BROWSER_WAIT": _exec_wait,
    "BROWSER_READ_PAGE": _exec_read_page,
    "BROWSER_CLICK": _exec_click,
    "BROWSER_FILL": _exec_fill,
    "BROWSER_FIND": _exec_find,
    "BROWSER_SUBMIT": _exec_submit,          # always raises
    "BROWSER_UPLOAD_FILE": _exec_upload_file,
}


def execute_directive(raw: str) -> dict:
    """Execute one directive string. Returns a result dict with keys:
      directive   — the original string
      kind        — parsed BROWSER_* kind
      outcome     — 'ok' | 'error' | 'not_found' | 'refused'
      text        — page text (for READ_PAGE) or empty
      error       — error message if outcome != 'ok'
      + kind-specific extras
    Raises BridgeSubmitRefused for BROWSER_SUBMIT (never swallowed).
    Raises BridgeError if bridge is unreachable."""
    kind, payload = _parse_directive(raw)
    handler = _HANDLERS.get(kind)
    if not handler:
        return {
            "directive": raw,
            "kind": kind,
            "outcome": "error",
            "error": f"unknown directive kind: {kind!r}",
        }
    result = handler(payload)
    result["directive"] = raw
    result["kind"] = kind
    return result


def execute_directives(directives: list[str]) -> list[dict]:
    """Execute a list of directives in order. Stops on first error.
    BridgeSubmitRefused propagates immediately (caller must handle it).
    Returns list of result dicts (same length as input on success,
    shorter on error — last entry has outcome='error')."""
    results = []
    for d in directives:
        r = execute_directive(d)  # BridgeSubmitRefused propagates
        results.append(r)
        if r.get("outcome") == "error":
            break  # stop on first error
    return results


def bridge_alive() -> bool:
    """Returns True if the sensei bridge responds."""
    r = _http("GET", "/extension/queue_state", timeout=1.5)
    return bool(r.get("ok"))


# ─── CLI self-test ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print("bridge_bridge.py — directive executor\n")
    print(f"Bridge alive: {bridge_alive()}")
    if len(sys.argv) > 1:
        directive = " ".join(sys.argv[1:])
        print(f"Executing: {directive!r}")
        try:
            r = execute_directive(directive)
            print(json.dumps(r, indent=2, default=str))
        except BridgeSubmitRefused as e:
            print(f"SUBMIT REFUSED (safety gate): {e}")
