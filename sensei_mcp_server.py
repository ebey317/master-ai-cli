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
from typing import Optional, Tuple, Iterator

BRIDGE = "http://127.0.0.1:8080"
DEFAULT_SESSION = "mcp-default"
WAIT_SECONDS = 30
POLL_MS = 400
CLAF_HEALTHZ = os.environ.get("CLAF_URL", "http://127.0.0.1:8000").rstrip("/") + "/healthz"
OLLAMA_TAGS = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/tags"
LOG_PATH = os.environ.get("SENSEI_LOG", os.path.expanduser("~/scripts/sensei_mcp.log"))

# Mirror stderr to log file — same pattern as secretary_agent.py
try:
    _log_file = open(LOG_PATH, "a", buffering=1)
    _orig_stderr = sys.stderr

    class _Tee:
        def write(self, s):
            _orig_stderr.write(s)
            _log_file.write(s)
        def flush(self):
            _orig_stderr.flush()
            _log_file.flush()
        def __getattr__(self, name):
            return getattr(_orig_stderr, name)

    sys.stderr = _Tee()
except Exception:
    pass


def _log(msg: str):
    sys.stderr.write(f"[sensei] {msg}\n")
    sys.stderr.flush()


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

def _claf_alive():
    r = _http_abs("GET", CLAF_HEALTHZ, timeout=1.5)
    return bool(r.get("ok"))

def _ollama_alive():
    r = _http_abs("GET", OLLAMA_TAGS, timeout=1.5)
    return bool(r.get("ok"))

def _http_abs(method, url, body=None, timeout=5.0):
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


def _push(action, session=DEFAULT_SESSION):
    _log(f"bridge push {action.get('kind')} session={session}")
    body = {"session_id": session, "actions": [action]}
    return _http("POST", "/extension/queue", body=body, timeout=3.0)


def _await_result(action_id, session=DEFAULT_SESSION, wait_seconds=WAIT_SECONDS):
    """Poll the bridge for a completed result. Returns the result dict or a
    timeout shape. Does not raise."""
    if not action_id:
        return {"ok": False, "reason": "no_action_id"}
    _log(f"await result action_id={action_id} timeout={wait_seconds}s")
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
            # Only resolve when bridge confirms done (ok:true + result present).
            # Pending responses also carry action_id so can't use that as signal.
            if j.get("ok") and j.get("result") is not None:
                _log(f"result received action_id={action_id}")
                return j
            last = j
        time.sleep(POLL_MS / 1000.0)
    _log(f"await timeout action_id={action_id}")
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
    _log(f"dispatch {kind} session={session}")
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

def tool_health(args):
    # Keep this small and deterministic: Claude Code uses it as a physical
    # "is the domino chain wired" check.
    bridge = _bridge_alive()
    claf = _claf_alive()
    ollama = _ollama_alive()
    rep = {
        "ok": True,
        "bridge": bridge,
        "claf": claf,
        "ollama": ollama,
        "session_default": DEFAULT_SESSION,
    }
    return {"content": [{"type": "text", "text": json.dumps(rep)}]}


DEFAULT_BROWSE_URL = os.environ.get("SENSEI_DEFAULT_URL", "https://www.google.com")


def tool_browse(args):
    url = str(args.get("url") or "").strip()
    if not url:
        url = DEFAULT_BROWSE_URL
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


def tool_screenshot(args):
    """Take a screenshot of the current browser page. Returns file path to saved PNG."""
    import base64, tempfile, time, urllib.request, urllib.error
    bridge = BRIDGE
    session = DEFAULT_SESSION

    # 1. Queue the action directly via the bridge HTTP API
    payload = json.dumps({
        "session_id": session,
        "actions": [{"kind": "BROWSER_SCREENSHOT", "session_id": session}]
    }).encode()
    try:
        req = urllib.request.Request(
            f"{bridge}/extension/queue",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            queued = json.loads(r.read())
    except Exception as e:
        return {"content": [{"type": "text", "text": f"screenshot queue error: {e}"}]}

    action_id = (queued.get("action_ids") or [""])[0]
    if not action_id:
        return {"content": [{"type": "text", "text": f"screenshot: no action_id returned: {queued}"}]}

    # 2. Poll for result (up to 12 s)
    deadline = time.time() + 12
    rec = None
    while time.time() < deadline:
        time.sleep(1)
        try:
            url = f"{bridge}/extension/result?action_id={action_id}&session_id={session}"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            if data.get("ok") and data.get("status") != "pending":
                rec = data
                break
            if data.get("ok") is False and data.get("status") != "pending":
                break
        except Exception:
            pass

    if rec is None:
        return {"content": [{"type": "text", "text": "screenshot timed out waiting for bridge result"}]}

    # 3. Walk the result tree to find b64
    def _find_b64(obj):
        if isinstance(obj, dict):
            if obj.get("b64"):
                return obj["b64"]
            for v in obj.values():
                found = _find_b64(v)
                if found:
                    return found
        return ""

    b64 = _find_b64(rec)
    if not b64:
        return {"content": [{"type": "text", "text": f"screenshot: no b64 in result: {json.dumps(rec)[:400]}"}]}

    tmp = tempfile.mktemp(suffix=".png", prefix="/tmp/sensei_")
    try:
        with open(tmp, "wb") as f:
            f.write(base64.b64decode(b64))
        return {"content": [{"type": "text", "text": f"screenshot saved: {tmp}"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"screenshot decode error: {e}"}]}


def tool_scroll(args):
    """Scroll the current browser page. direction: up/down/top/bottom or pixel amount."""
    direction = str(args.get("direction") or "down").strip()
    out = _dispatch("BROWSER_SCROLL", {"target": direction})
    rep = f"scroll '{direction}' -> {json.dumps(out)[:300]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_js_eval(args):
    """Execute JavaScript in the current browser page. Returns result as text."""
    code = str(args.get("code") or "").strip()
    if not code:
        return {"content": [{"type": "text", "text": "js_eval: code is required"}]}
    out = _dispatch("BROWSER_JS", {"target": code})
    result = out.get("result", {})
    inner = result.get("result", result)
    val = inner.get("result", inner) if isinstance(inner, dict) else inner
    rep = json.dumps(val) if not isinstance(val, str) else val
    if len(rep) > 2000:
        rep = rep[:2000] + " ...[truncated]"
    return {"content": [{"type": "text", "text": rep}]}


def tool_photos_search(args):
    """Navigate to Google Photos search and extract image URLs from the JS-rendered page."""
    query = str(args.get("query") or "biovega").strip()
    wait_sec = int(args.get("wait") or 4)
    url = f"https://photos.google.com/search/{query.replace(' ', '%20')}"
    # navigate
    nav = _dispatch("BROWSER_NAV", {"target": url})
    if not nav.get("ok"):
        return {"content": [{"type": "text", "text": f"photos_search: nav failed -> {json.dumps(nav)[:300]}"}]}
    # wait for JS to render thumbnails
    import time
    time.sleep(wait_sec)
    # extract image urls
    out = _dispatch("BROWSER_READ_IMAGES", {})
    result = out.get("result", {})
    # bridge wraps: result -> result -> final_state -> {count, images}
    inner = result.get("result", result)
    final_state = inner.get("final_state", inner)
    images = final_state.get("images") or []
    count = final_state.get("count", len(images))
    if not images:
        raw = json.dumps(out)[:800]
        return {"content": [{"type": "text", "text": f"photos_search '{query}': 0 images found. raw={raw}"}]}
    lines = [f"photos_search '{query}' at {url} -> {count} images found:"]
    for i, img in enumerate(images[:40]):
        lines.append(f"  [{i}] {img.get('url','')} ({img.get('w',0)}x{img.get('h',0)})")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def _resolve_profile_path(profile: dict, key_path: str):
    """Resolve a dot-notation key path into a profile value.

    "personal.first_name" → profile["personal"]["first_name"]
    "experience.0.employer" → profile["experience"][0]["employer"]
    Returns None if any segment is missing.
    """
    node = profile
    for segment in key_path.split("."):
        if isinstance(node, list):
            try:
                node = node[int(segment)]
            except (IndexError, ValueError):
                return None
        elif isinstance(node, dict):
            node = node.get(segment)
        else:
            return None
        if node is None:
            return None
    return node if isinstance(node, str) else str(node) if node is not None else None


def tool_autofill_job_form(args):
    """Layer 3 — Code-First/LLM-Last autofill for job application forms.

    Phase 1 (code-fill): fingerprint the ATS, look up its selector map,
    fill every standard field from ~/.master_ai_profile.json without LLM involvement.
    Phase 2 result: return the list of unfilled fields (essay/custom questions)
    for the LLM to generate targeted answers.

    Args:
        dry_run (str): "true" to simulate without writing to the page.
        session (str): optional session id for the bridge (default: mcp-default).
        profile_path (str): override path to the profile JSON.
    """
    import os, sys, pathlib

    dry_run = str(args.get("dry_run") or "false").lower() in ("true", "1", "yes")
    session = str(args.get("session") or DEFAULT_SESSION)
    profile_path = str(args.get("profile_path") or os.path.expanduser("~/.master_ai_profile.json"))

    # ── Load master profile ───────────────────────────────────────────────────
    try:
        with open(profile_path, encoding="utf-8") as f:
            profile = json.load(f)
    except FileNotFoundError:
        return {"content": [{"type": "text", "text":
            f"autofill_job_form: profile not found at {profile_path}. "
            "Operator must curate ~/.master_ai_profile.json first."}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"autofill_job_form: profile load error: {e}"}]}

    # ── Read current page URL + content for fingerprinting ───────────────────
    read_out = _dispatch("BROWSER_READ", {}, session=session, wait=20)
    page_text = ""
    current_url = ""
    try:
        res = read_out.get("result", {})
        inner = res.get("result", res)
        final = inner.get("final_state", inner) if isinstance(inner, dict) else {}
        page_text = final.get("content") or final.get("text") or ""
        current_url = final.get("url") or ""
    except Exception:
        pass

    # ── Fingerprint ATS ───────────────────────────────────────────────────────
    _scripts_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _scripts_dir)
    try:
        from ats_fingerprint import fingerprint_ats
        from ats_maps import greenhouse, lever
    except ImportError as e:
        return {"content": [{"type": "text", "text": f"autofill_job_form: import error: {e}"}]}

    ats = fingerprint_ats(page_text, url=current_url)

    ats_map_module = {
        "greenhouse": greenhouse,
        "lever":      lever,
    }.get(ats)

    if ats_map_module is None:
        return {"content": [{"type": "text", "text":
            f"autofill_job_form: ATS detected as '{ats}' — "
            "no selector map available (workday is Phase 2; unknown = cannot autofill). "
            f"URL: {current_url or '(could not read)'}\n"
            "Hand off to LLM-driven fill or operator."}]}

    selector_map: dict = ats_map_module.SELECTORS
    field_types: dict = ats_map_module.FIELD_TYPES

    # ── Phase 1: code-fill via deterministic selector map ────────────────────
    filled: list[dict] = []
    skipped_no_value: list[dict] = []
    skipped_file: list[dict] = []

    for selector, key_path in selector_map.items():
        value = _resolve_profile_path(profile, key_path)

        # Placeholder values (start with "?") count as missing
        if value and value.startswith("?"):
            value = None

        if not value:
            skipped_no_value.append({"selector": selector, "key": key_path})
            continue

        field_type = field_types.get(key_path, "text")

        if field_type == "file":
            # File uploads require operator confirmation — collect and report
            skipped_file.append({"selector": selector, "key": key_path, "value": value})
            continue

        if dry_run:
            filled.append({"selector": selector, "key": key_path,
                           "value": value[:60], "dry_run": True})
            continue

        # Dispatch BROWSER_FILL with CSS selector as target
        fill_out = _dispatch("BROWSER_FILL", {
            "target": selector,
            "value": value,
            "field_type": field_type,
        }, session=session, wait=15)

        ok = fill_out.get("ok", False)
        filled.append({
            "selector": selector,
            "key": key_path,
            "value": value[:60],
            "ok": ok,
            "error": fill_out.get("result", {}).get("error") if not ok else None,
        })

    # ── Compose report ────────────────────────────────────────────────────────
    lines = [
        f"autofill_job_form {'(DRY RUN) ' if dry_run else ''}— ATS: {ats}",
        f"URL: {current_url or '(unknown)'}",
        f"Fields filled: {len(filled)}",
        f"Fields skipped (no profile value): {len(skipped_no_value)}",
        f"File uploads (need operator action): {len(skipped_file)}",
        "",
    ]

    if filled:
        lines.append("Filled:")
        for f in filled:
            status = "(dry)" if f.get("dry_run") else ("OK" if f.get("ok") else f"ERR: {f.get('error','?')}")
            lines.append(f"  [{status}]  {f['key']} = \"{f['value']}\"")
        lines.append("")

    if skipped_file:
        lines.append("Resume / file uploads (operator must upload manually or use file_upload tool):")
        for f in skipped_file:
            lines.append(f"  {f['key']} → {f['selector']} (value: {f['value'][:80]})")
        lines.append("")

    if skipped_no_value:
        lines.append("Profile fields missing (operator should fill ~/.master_ai_profile.json):")
        for f in skipped_no_value[:10]:
            lines.append(f"  {f['key']} ({f['selector']})")
        if len(skipped_no_value) > 10:
            lines.append(f"  ... and {len(skipped_no_value) - 10} more")
        lines.append("")

    lines.append("Phase 2 — LLM should now generate answers for any remaining empty fields")
    lines.append("(use 'read' to get the current list of unfilled inputs).")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def tool_key_press(args):
    """Send a keyboard key to the focused element. key: Tab, Enter, Escape, ArrowDown, etc."""
    key = str(args.get("key") or "").strip()
    if not key:
        return {"content": [{"type": "text", "text": "key_press: key is required (e.g. Tab, Enter, Escape, ArrowDown)"}]}
    out = _dispatch("BROWSER_KEY", {"target": key})
    rep = f"key_press '{key}' -> {json.dumps(out)[:300]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_read_full(args):
    """Read the FULL accessibility tree of the current page including all interactive elements and ref IDs.
    Use this when read() truncates and you need to see all form fields."""
    out = _dispatch("BROWSER_READ_PAGE_FULL", {}, wait=20)
    rep = json.dumps(out)
    if len(rep) > 8000:
        rep = rep[:8000] + " ...[truncated]"
    return {"content": [{"type": "text", "text": rep}]}


def tool_double_click(args):
    """Double-click an element by visible label or selector."""
    what = str(args.get("what") or "").strip()
    if not what:
        return {"content": [{"type": "text", "text": "double_click: what is required"}]}
    out = _dispatch("BROWSER_DOUBLE_CLICK", {"target": what})
    rep = f"double_click '{what}' -> {json.dumps(out)[:300]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_upload_file(args):
    """Upload a file from the local filesystem to a file input on the page.
    selector: CSS selector or label for the file input. path: absolute path to the file."""
    selector = str(args.get("selector") or "").strip()
    path = str(args.get("path") or "").strip()
    if not selector or not path:
        return {"content": [{"type": "text", "text": "upload_file: selector and path are required"}]}
    target = f"{selector} :: {path}"
    out = _dispatch("BROWSER_UPLOAD_FILE", {"target": target}, wait=20)
    rep = f"upload_file '{selector}' <- '{path}' -> {json.dumps(out)[:400]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_wait(args):
    """Pause for a number of milliseconds (100-15000) before the next action."""
    ms = int(args.get("ms") or 1000)
    ms = max(100, min(ms, 15000))
    out = _dispatch("BROWSER_WAIT", {"target": str(ms)})
    rep = f"wait {ms}ms -> {json.dumps(out)[:200]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_batch(args):
    """Execute a sequence of browser actions atomically. actions: JSON array of objects
    with fields: kind (click/fill/key/scroll/wait/nav), target, value (for fill).
    Example: [{"kind":"click","target":"Submit"},{"kind":"key","target":"Enter"}]"""
    raw = args.get("actions") or "[]"
    if isinstance(raw, str):
        try:
            actions = json.loads(raw)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"batch: actions must be valid JSON array: {e}"}]}
    elif isinstance(raw, list):
        actions = raw
    else:
        return {"content": [{"type": "text", "text": "batch: actions must be a JSON array"}]}

    KIND_MAP = {
        "click": "BROWSER_CLICK",
        "fill": "BROWSER_FILL",
        "key": "BROWSER_KEY",
        "scroll": "BROWSER_SCROLL",
        "wait": "BROWSER_WAIT",
        "nav": "BROWSER_NAV",
        "navigate": "BROWSER_NAV",
        "double_click": "BROWSER_DOUBLE_CLICK",
        "hover": "BROWSER_HOVER",
        "read": "BROWSER_READ_PAGE",
        "screenshot": "BROWSER_SCREENSHOT",
    }

    results = []
    for i, action in enumerate(actions):
        kind_raw = str(action.get("kind") or "").lower()
        kind = KIND_MAP.get(kind_raw, kind_raw.upper())
        target = str(action.get("target") or "")
        value = str(action.get("value") or "")
        payload = {"target": f"{target} :: {value}" if (kind == "BROWSER_FILL" and value) else target}
        out = _dispatch(kind, payload)
        ok = out.get("ok", True)
        results.append({"step": i + 1, "kind": kind, "target": target[:60], "ok": ok,
                         "error": out.get("result", {}).get("error") if not ok else None})
        if not ok:
            results.append({"step": "STOPPED", "reason": f"step {i+1} failed"})
            break

    lines = [f"batch: {len(results)} steps executed"]
    for r in results:
        if "reason" in r:
            lines.append(f"  → STOP: {r['reason']}")
        else:
            status = "✓" if r["ok"] else f"✗ {r.get('error','?')}"
            lines.append(f"  {r['step']}. [{status}] {r['kind']}: {r['target']}")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def tool_hover(args):
    """Hover over an element to reveal tooltips, dropdowns, or hover states."""
    what = str(args.get("what") or "").strip()
    if not what:
        return {"content": [{"type": "text", "text": "hover: what is required"}]}
    out = _dispatch("BROWSER_HOVER", {"target": what})
    rep = f"hover '{what}' -> {json.dumps(out)[:300]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_right_click(args):
    """Right-click an element to open its context menu."""
    what = str(args.get("what") or "").strip()
    if not what:
        return {"content": [{"type": "text", "text": "right_click: what is required"}]}
    out = _dispatch("BROWSER_RIGHT_CLICK", {"target": what})
    return {"content": [{"type": "text", "text": f"right_click '{what}' -> {json.dumps(out)[:300]}"}]}


def tool_tab_list(args):
    """List all open browser tabs with their IDs, titles, and URLs."""
    out = _dispatch("BROWSER_TAB_LIST", {})
    rep = json.dumps(out)
    if len(rep) > 3000:
        rep = rep[:3000] + " ...[truncated]"
    return {"content": [{"type": "text", "text": rep}]}


def tool_tab_create(args):
    """Open a new browser tab. url: the URL to navigate to (optional — blank tab if omitted)."""
    url = str(args.get("url") or "").strip()
    out = _dispatch("BROWSER_TAB_CREATE", {"target": url or "about:blank"})
    rep = f"tab_create url='{url}' -> {json.dumps(out)[:300]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_tab_close(args):
    """Close a browser tab by its numeric tab ID (from tab_list)."""
    tab_id = str(args.get("tab_id") or "").strip()
    if not tab_id:
        return {"content": [{"type": "text", "text": "tab_close: tab_id is required (get it from tab_list)"}]}
    out = _dispatch("BROWSER_TAB_CLOSE", {"target": tab_id})
    rep = f"tab_close id={tab_id} -> {json.dumps(out)[:200]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_console_logs(args):
    """Read captured browser console messages. pattern: optional substring filter (e.g. 'error')."""
    pattern = str(args.get("pattern") or "").strip()
    out = _dispatch("BROWSER_CONSOLE", {"target": pattern or "all"})
    rep = json.dumps(out)
    if len(rep) > 4000:
        rep = rep[:4000] + " ...[truncated]"
    return {"content": [{"type": "text", "text": rep}]}


def tool_network_requests(args):
    """Read recent network requests made by the current page. url_pattern: optional URL filter substring."""
    url_pattern = str(args.get("url_pattern") or "").strip()
    out = _dispatch("BROWSER_NETWORK", {"target": url_pattern or "all"})
    rep = json.dumps(out)
    if len(rep) > 4000:
        rep = rep[:4000] + " ...[truncated]"
    return {"content": [{"type": "text", "text": rep}]}


def tool_resize_window(args):
    """Resize the browser window. width and height in pixels."""
    width = int(args.get("width") or 1280)
    height = int(args.get("height") or 900)
    out = _dispatch("BROWSER_RESIZE_WINDOW", {"target": f"{width}x{height}"})
    rep = f"resize_window {width}x{height} -> {json.dumps(out)[:200]}"
    return {"content": [{"type": "text", "text": rep}]}


def tool_drag(args):
    """Drag from one element to another (drag and drop). from_target: source element label/selector.
    to_target: destination element label/selector."""
    from_target = str(args.get("from_target") or "").strip()
    to_target = str(args.get("to_target") or "").strip()
    if not from_target or not to_target:
        return {"content": [{"type": "text", "text": "drag: from_target and to_target are required"}]}
    out = _dispatch("BROWSER_DRAG", {"target": from_target, "to": to_target})
    rep = f"drag '{from_target}' → '{to_target}' -> {json.dumps(out)[:300]}"
    return {"content": [{"type": "text", "text": rep}]}


HANDLERS = {
    "chat": tool_chat,
    "health": tool_health,
    "browse": tool_browse,
    "click": tool_click,
    "fill": tool_fill,
    "read": tool_read,
    "search": tool_search,
    "run": tool_run,
    "write_file": tool_write_file,
    "read_file": tool_read_file,
    "photos_search": tool_photos_search,
    "screenshot": tool_screenshot,
    "scroll": tool_scroll,
    "js_eval": tool_js_eval,
    "autofill_job_form": tool_autofill_job_form,
    # Phase 2 — parity additions
    "key_press": tool_key_press,
    "read_full": tool_read_full,
    "double_click": tool_double_click,
    "upload_file": tool_upload_file,
    "wait": tool_wait,
    "batch": tool_batch,
    "hover": tool_hover,
    "right_click": tool_right_click,
    "tab_list": tool_tab_list,
    "tab_create": tool_tab_create,
    "tab_close": tool_tab_close,
    "console_logs": tool_console_logs,
    "network_requests": tool_network_requests,
    "resize_window": tool_resize_window,
    "drag": tool_drag,
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
        "name": "health",
        "description": "Report whether bridge/CLAF/Ollama are reachable (wiring check).",
        "inputSchema": {
            "type": "object",
            "properties": {},
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
    {
        "name": "photos_search",
        "description": "Search Google Photos for images and return their URLs. Navigates to the authenticated Google Photos search page and extracts image URLs from the rendered thumbnails.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "wait": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "screenshot",
        "description": "Take a screenshot of the current browser page. Returns path to saved PNG file.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the current browser page. Use direction: up, down, top, bottom, or a pixel number.",
        "inputSchema": {
            "type": "object",
            "properties": {"direction": {"type": "string"}},
            "required": ["direction"],
        },
    },
    {
        "name": "js_eval",
        "description": "Execute JavaScript in the current browser page and return the result. Use for DOM inspection, data extraction, or any page manipulation.",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    # ── Phase 2 parity tools ────────────────────────────────────────────────
    {
        "name": "key_press",
        "description": "Send a keyboard key to the active/focused element. Use for Tab, Enter, Escape, ArrowDown, ArrowUp, Backspace, etc.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Key name: Tab, Enter, Escape, ArrowDown, ArrowUp, Backspace, Delete, Space, Home, End, PageDown, PageUp, or any character."}},
            "required": ["key"],
        },
    },
    {
        "name": "read_full",
        "description": "Read the FULL accessibility tree of the current page — all interactive elements, ref IDs, hidden fields. Use when read() truncates.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "double_click",
        "description": "Double-click an element by visible label or CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {"what": {"type": "string"}},
            "required": ["what"],
        },
    },
    {
        "name": "upload_file",
        "description": "Upload a local file to a file input element on the page. selector: CSS selector or label for the input. path: absolute filesystem path to the file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector or label for the file input element."},
                "path": {"type": "string", "description": "Absolute path to the file on the local machine."},
            },
            "required": ["selector", "path"],
        },
    },
    {
        "name": "wait",
        "description": "Pause for ms milliseconds (100–15000) before the next action. Use after page loads or animations.",
        "inputSchema": {
            "type": "object",
            "properties": {"ms": {"type": "string", "description": "Milliseconds to wait (100–15000)."}},
            "required": ["ms"],
        },
    },
    {
        "name": "batch",
        "description": "Execute multiple browser actions in sequence atomically. actions: JSON array, each item has kind (click/fill/key/scroll/wait/nav/hover/double_click), target, and optional value (for fill).",
        "inputSchema": {
            "type": "object",
            "properties": {"actions": {"type": "string", "description": "JSON array of action objects. Each: {\"kind\":\"click\",\"target\":\"Submit\"} or {\"kind\":\"fill\",\"target\":\"Email\",\"value\":\"me@example.com\"} or {\"kind\":\"key\",\"target\":\"Tab\"}."}},
            "required": ["actions"],
        },
    },
    {
        "name": "hover",
        "description": "Hover the mouse over an element to reveal tooltips, dropdown menus, or hover states.",
        "inputSchema": {
            "type": "object",
            "properties": {"what": {"type": "string", "description": "Element label, visible text, or CSS selector to hover over."}},
            "required": ["what"],
        },
    },
    {
        "name": "right_click",
        "description": "Right-click an element to open its context menu. Dispatches mousedown+mouseup+contextmenu with button=2.",
        "inputSchema": {
            "type": "object",
            "properties": {"what": {"type": "string", "description": "Element label, visible text, or CSS selector to right-click."}},
            "required": ["what"],
        },
    },
    {
        "name": "tab_list",
        "description": "List all open browser tabs with their numeric IDs, titles, and URLs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "tab_create",
        "description": "Open a new browser tab and navigate to a URL. Returns the new tab's ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to open in the new tab. Omit for a blank tab."}},
            "required": [],
        },
    },
    {
        "name": "tab_close",
        "description": "Close a browser tab by its numeric tab ID. Get IDs from tab_list.",
        "inputSchema": {
            "type": "object",
            "properties": {"tab_id": {"type": "string", "description": "Numeric tab ID from tab_list."}},
            "required": ["tab_id"],
        },
    },
    {
        "name": "console_logs",
        "description": "Read browser console messages (console.log, errors, warnings) from the current page. pattern: optional substring filter.",
        "inputSchema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "Optional substring to filter log messages (e.g. 'error', 'warning')."}},
            "required": [],
        },
    },
    {
        "name": "network_requests",
        "description": "Read recent HTTP network requests made by the current page. url_pattern: optional URL substring filter.",
        "inputSchema": {
            "type": "object",
            "properties": {"url_pattern": {"type": "string", "description": "Optional URL substring to filter requests (e.g. '/api/', 'example.com')."}},
            "required": [],
        },
    },
    {
        "name": "resize_window",
        "description": "Resize the browser window to specified pixel dimensions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "string", "description": "Window width in pixels."},
                "height": {"type": "string", "description": "Window height in pixels."},
            },
            "required": ["width", "height"],
        },
    },
    {
        "name": "drag",
        "description": "Drag one element and drop it onto another. Use for file drop zones, sortable lists, sliders.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_target": {"type": "string", "description": "Label or selector of the element to drag from."},
                "to_target": {"type": "string", "description": "Label or selector of the element to drop onto."},
            },
            "required": ["from_target", "to_target"],
        },
    },
    # ── End Phase 2 parity tools ─────────────────────────────────────────────
    {
        "name": "autofill_job_form",
        "description": (
            "Code-first autofill for job application forms (Greenhouse, Lever). "
            "Phase 1: fingerprints the ATS on the current page, fills all standard fields "
            "(name, email, phone, LinkedIn, work auth, EEO) from ~/.master_ai_profile.json "
            "without LLM involvement. "
            "Phase 2 output: returns the list of unfilled custom/essay fields so the LLM "
            "can generate targeted answers for just those 2-3 fields. "
            "Use dry_run=true to preview without writing to the page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "string",
                    "description": "Set to 'true' to simulate fill without writing to the page.",
                },
                "session": {
                    "type": "string",
                    "description": "Bridge session id (default: mcp-default).",
                },
                "profile_path": {
                    "type": "string",
                    "description": "Override path to the master profile JSON (default: ~/.master_ai_profile.json).",
                },
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC stdio loop
# ---------------------------------------------------------------------------

def _send(obj, *, framed: bool = False):
    payload = json.dumps(obj).encode("utf-8")
    if framed:
        sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(payload.decode("utf-8") + "\n")
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
    for msg, framed in _iter_rpc_messages():
        try:
            resp = _handle(msg if isinstance(msg, dict) else {})
        except Exception as e:
            sys.stderr.write(f"[sensei] handler crash: {e}\n")
            sys.stderr.flush()
            resp = {"jsonrpc": "2.0", "id": (msg or {}).get("id"),
                    "error": {"code": -32603, "message": f"internal: {e}"}}
        if resp is not None:
            _send(resp, framed=framed)
    return 0

def _read_exact(n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _iter_rpc_messages() -> Iterator[Tuple[dict, bool]]:
    """Yield parsed JSON-RPC dicts from stdin.

    Supports both:
    - MCP/LSP-style framed messages: Content-Length: N\\r\\n\\r\\n{...}
    - newline-delimited JSON (older local scripts / ad-hoc tests)
    """
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return
        line_str = line.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue

        # Framed: Content-Length: N
        if line_str.lower().startswith("content-length:"):
            try:
                n = int(line_str.split(":", 1)[1].strip())
            except Exception:
                sys.stderr.write(f"[sensei] bad content-length line: {line_str}\n")
                sys.stderr.flush()
                continue

            # Read headers until blank line
            while True:
                h = sys.stdin.buffer.readline()
                if not h:
                    return
                if h in (b"\r\n", b"\n"):
                    break

            raw = _read_exact(n)
            if not raw:
                return
            try:
                yield json.loads(raw.decode("utf-8", errors="replace")), True
            except Exception as e:
                sys.stderr.write(f"[sensei] parse error: {e}\n")
                sys.stderr.flush()
            continue

        # Newline-delimited JSON
        try:
            yield json.loads(line_str), False
        except Exception as e:
            sys.stderr.write(f"[sensei] parse error: {e}\n")
            sys.stderr.flush()
            continue


if __name__ == "__main__":
    sys.exit(main())
