"""Runway Dev API client for Master AI (Sensei).

Built 2026-10-04 following https://docs.dev.runwayml.com/ai-context.md.

Covers the submit-and-poll lifecycle every Runway generation uses:
  POST -> {id} -> poll GET /v1/tasks/{id} until SUCCEEDED/FAILED.
Key comes from RUNWAYML_API_SECRET (keychain: ~/.master_ai_keys row
RUNWAYML_API_SECRET). Every request carries the mandatory
X-Runway-Version header (2024-11-06). Output URLs are temporary —
download_artifact() stores what you keep.

Per-model pitfalls encoded here (do not "simplify" them away):
- Bodies are discriminated unions keyed on `model`; ratio/duration rules
  differ per model (see model_presets()).
- THROTTLED is queued, not failed. FAILED carries failure/failureCode.
- Never invent model ids; use ones in model_presets() or the models guide.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://api.dev.runwayml.com"
VERSION = "2024-11-06"
KEYCHAIN = Path.home() / ".master_ai_keys"

# Model presets from /guides/models.md + /guides/pricing.md (2026-10-04).
# ratio/duration fields list what each model ACCEPTS on text_to_video;
# prices are credits/second of output video (media-resolution noted).
MODEL_PRESETS = {
    "gen4.5":        {"io": "text/image->video", "ratios": ["1280:720", "720:1280"],
                      "duration": (2, 10), "credits_per_s": 12,
                      "prompt_chars": 1000, "required": ["ratio", "duration"]},
    "gen4_turbo":    {"io": "image->video", "credits_per_s": 5},
    "aleph2":        {"io": "video+text->video", "credits_per_s": 28, "min_gen": 56},
    "act_two":       {"io": "image/video->video", "credits_per_s": 5},
    "veo3.1":        {"io": "text/image->video", "credits_per_s": "20 no-audio / 40 audio"},
    "veo3.1_fast":   {"io": "text/image->video", "credits_per_s": "10 no-audio / 15 audio"},
    "seedance2":     {"io": "text/image/video->video", "credits_per_s": "36-40 (4K: 150)"},
    "seedance2_fast":{"io": "text/image/video->video", "credits_per_s": 29},
    "seedance2_mini":{"io": "text/image/video->video", "credits_per_s": 16, "min_gen": 64},
    "wan3":          {"io": "text/image->video", "credits_per_s": "5 (480p) - 20 (1080p)"},
    "hailuo3":       {"io": "text/image/video->video", "credits_per_s": "10 (768p) - 15 (2K)"},
    "gemini_omni_flash": {"io": "text/image/video->video", "credits_per_s": 10,
                          "prompt_chars": 4000},
    "gen4_image":      {"io": "text/image->image", "credits_per_image": "5 (720p) / 8 (1080p)"},
    "gen4_image_turbo":{"io": "text/image->image", "credits_per_image": 2},
    "eleven_v3":       {"io": "text->audio (TTS)", "pricing": "1 credit / 50 chars"},
    "eleven_text_to_sound_v2": {"io": "text->sfx", "pricing": "1 credit/s"},
}


def _load_key() -> str:
    """RUNWAYML_API_SECRET from env, else from the master-ai keychain file."""
    key = os.environ.get("RUNWAYML_API_SECRET")
    if key:
        return key.strip()
    if KEYCHAIN.exists():
        for line in KEYCHAIN.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("RUNWAYML_API_SECRET="):
                val = line.partition("=")[2].strip()
                if val and "<" != val[0] and "[" != val[0] and val.isascii():
                    return val
    raise RuntimeError(
        "RUNWAYML_API_SECRET not set. Add a RUNWAYML_API_SECRET=... row to "
        f"{KEYCHAIN} (chmod 0400) or export the env var. "
        "Get the key at https://dev.runway.com/ Developer Portal."
    )


def _headers(extra: dict | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {_load_key()}",
        "X-Runway-Version": VERSION,
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _request(method: str, path: str, body: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=_headers(),
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
        except Exception:
            detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Runway HTTP {e.code} on {method} {path}: {detail}") from e


def start_task(endpoint: str, body: dict) -> str:
    """Submit a generation. Returns the task id (NOT the result)."""
    r = _request("POST", endpoint, body)
    return r["id"]


def get_task(task_id: str) -> dict:
    return _request("GET", f"/v1/tasks/{task_id}")


def cancel_task(task_id: str) -> None:
    _request("DELETE", f"/v1/tasks/{task_id}")


def wait_for_task(task_id: str, max_seconds: int = 900, poll_every: int = 6) -> dict:
    """Poll with backoff until terminal status. Matches SDK wait_for_task_output."""
    deadline = time.time() + max_seconds
    delay = poll_every
    while time.time() < deadline:
        t = get_task(task_id)
        status = t.get("status")
        if status == "SUCCEEDED":
            return t
        if status in ("FAILED", "CANCELLED"):
            raise RuntimeError(
                f"Task {task_id} terminal: {status} "
                f"code={t.get('failureCode')} detail={t.get('failure')}"
            )
        # PENDING / THROTTLED / RUNNING -> keep waiting (THROTTLED is queued, not failed)
        time.sleep(delay)
        delay = min(delay * 1.3, 20)
    raise TimeoutError(f"Task {task_id} not terminal after {max_seconds}s")


def generate(endpoint: str, body: dict, max_seconds: int = 900) -> dict:
    """Submit + wait. Returns the finished task dict (has 'output' URLs)."""
    task_id = start_task(endpoint, body)
    print(f"task {task_id} started on {endpoint} ...")
    return wait_for_task(task_id, max_seconds=max_seconds)


def get_credits() -> dict:
    """Organization credit balance — a free, non-generating call; good first test."""
    return _request("GET", "/v1/organization")


def download_artifact(url: str, dest: str | Path) -> Path:
    """Output URLs are temporary — persist anything you keep."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        org = get_credits()
        print(json.dumps(org, indent=2)[:500])
    else:
        print("usage: python3 runway_client.py --check   (credit balance; verifies key)")