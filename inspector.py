#!/usr/bin/env python3
"""inspector.py — Semantic exit validator for CLAF agent loops.

When an agent claims completion, this module:
1. Captures evidence (screenshot via sensei bridge OR reads an output file)
2. Sends evidence + original goal to a FRESH local Ollama instance (no history)
3. Gets a YES/NO verdict
4. Returns (passed: bool, reason: str) to the caller

The caller must kick the agent back into the loop if passed=False.

Usage:
    from inspector import inspect_completion
    passed, reason = inspect_completion(
        goal="Fill out the job application for BGIS on ZipRecruiter",
        evidence_type="screenshot",   # or "file"
        evidence_path=None,           # required if evidence_type="file"
    )
    if not passed:
        # feed reason back as error message, re-enter loop
"""

import base64
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434"
VISION_MODEL = os.environ.get("INSPECTOR_VISION_MODEL", "llava:latest")
TEXT_MODEL = os.environ.get("INSPECTOR_TEXT_MODEL", "qwen2.5:3b")
BRIDGE_URL = "http://localhost:8080"


# ── evidence capture ─────────────────────────────────────────────────────────


def _capture_screenshot() -> str | None:
    """Ask the sensei bridge for a screenshot. Returns local PNG path or None."""
    try:
        action_id = f"inspector_{int(time.time() * 1000)}"
        payload = json.dumps(
            {"action_id": action_id, "action": "BROWSER_SCREENSHOT", "args": {}}
        ).encode()
        req = urllib.request.Request(
            f"{BRIDGE_URL}/queue",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            queued = json.loads(r.read())
        if not queued.get("action_id"):
            return None

        for _ in range(30):  # poll up to 15s
            time.sleep(0.5)
            req2 = urllib.request.Request(
                f"{BRIDGE_URL}/result/{action_id}", method="GET"
            )
            try:
                with urllib.request.urlopen(req2, timeout=5) as r2:
                    rec = json.loads(r2.read())
            except urllib.error.HTTPError:
                continue
            b64 = rec.get("screenshot") or rec.get("b64")
            if b64:
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".png", delete=False, prefix="inspector_"
                )
                tmp.write(base64.b64decode(b64))
                tmp.close()
                return tmp.name
        return None
    except Exception:
        return None


def _read_file_evidence(path: str) -> str | None:
    try:
        text = Path(path).read_text(errors="replace")
        return text[:4000]  # cap — don't bloat the inspector context
    except Exception:
        return None


# ── ollama calls ─────────────────────────────────────────────────────────────

_INSPECTOR_PROMPT = (
    "You are a strict pass/fail reviewer. "
    "Look at the evidence below and answer ONLY:\n"
    "Line 1: YES or NO\n"
    "Line 2: One sentence explaining why.\n\n"
    "Do not say anything else. Do not hedge. "
    "YES means the goal is fully complete and confirmed. "
    "NO means anything is missing, ambiguous, or unverified.\n\n"
    "GOAL: {goal}\n\n"
    "EVIDENCE:\n{evidence_label}"
)


def _ask_vision(goal: str, image_path: str) -> tuple[bool, str]:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    prompt = _INSPECTOR_PROMPT.format(goal=goal, evidence_label="[screenshot attached]")
    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 64},
    }
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.loads(r.read())["message"]["content"].strip()
        return _parse_verdict(text)
    except Exception as e:
        return False, f"inspector vision call failed: {e}"


def _ask_text(goal: str, file_content: str) -> tuple[bool, str]:
    prompt = _INSPECTOR_PROMPT.format(goal=goal, evidence_label=file_content[:3000])
    payload = {
        "model": TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 64},
    }
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.loads(r.read())["message"]["content"].strip()
        return _parse_verdict(text)
    except Exception as e:
        return False, f"inspector text call failed: {e}"


def _parse_verdict(text: str) -> tuple[bool, str]:
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return False, "inspector returned empty response"
    passed = lines[0].upper().startswith("YES")
    reason = lines[1] if len(lines) > 1 else "(no reason given)"
    return passed, reason


# ── public entry point ────────────────────────────────────────────────────────


def inspect_completion(
    goal: str,
    evidence_type: str = "screenshot",  # "screenshot" or "file"
    evidence_path: str | None = None,
) -> tuple[bool, str]:
    """
    Capture evidence and ask a fresh local model if the goal is done.

    Returns:
        (True,  reason) — goal confirmed complete
        (False, reason) — not done; feed reason back as the loop error
    """
    if evidence_type == "file":
        if not evidence_path:
            return False, "evidence_type=file but no evidence_path provided"
        content = _read_file_evidence(evidence_path)
        if content is None:
            return False, f"inspector could not read evidence file: {evidence_path}"
        return _ask_text(goal, content)

    # default: screenshot via sensei bridge
    image_path = _capture_screenshot()
    if image_path is None:
        return (
            False,
            "inspector: sensei bridge screenshot failed — cannot verify completion",
        )
    try:
        return _ask_vision(goal, image_path)
    finally:
        try:
            os.unlink(image_path)
        except Exception:
            pass
