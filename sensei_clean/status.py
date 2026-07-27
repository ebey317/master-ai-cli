"""
Last-clean status tracking.

Persists a tiny JSON record after every full scan/apply so the menu /
CLI can show:

  Last full scan : 2026-05-11 22:14
  Files seen     : 34,506
  Reclaim ready  : 30.6 MB
  Last run dir   : /home/user/sensei_runs/20260511_221456

State lives at ~/.config/sensei-clean/state.json (0600). Never contains
file names — only counts and totals, so it's safe to view at any time.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATE_DIR = Path.home() / ".config" / "sensei-clean"
STATE_FILE = STATE_DIR / "state.json"


def _ensure_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    _ensure_dir()
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


def record_full_scan(
    *,
    run_dir: str,
    total_items: int,
    total_bytes: int,
    reclaim_bytes: int,
    duplicate_clusters: int,
    sources: list[str],
) -> dict:
    """Called by the engine (or CLI) after a successful full scan.
    Returns the updated state dict."""
    state = load_state()
    now = int(time.time())
    state["last_full_scan_ts"] = now
    state["last_full_scan_iso"] = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(now)
    )
    state["last_run_dir"] = run_dir
    state["last_total_items"] = int(total_items)
    state["last_total_bytes"] = int(total_bytes)
    state["last_reclaim_bytes"] = int(reclaim_bytes)
    state["last_duplicate_clusters"] = int(duplicate_clusters)
    state["last_sources"] = list(sources)
    save_state(state)
    return state


def record_apply(*, run_dir: str, applied: int, failed: int) -> dict:
    state = load_state()
    state["last_apply_ts"] = int(time.time())
    state["last_apply_iso"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_apply_run_dir"] = run_dir
    state["last_apply_applied"] = int(applied)
    state["last_apply_failed"] = int(failed)
    save_state(state)
    return state


def format_status() -> str:
    """Human-readable status block used by the `status` subcommand."""
    state = load_state()
    if not state:
        return ("No Sensei Clean runs recorded yet.\n"
                "Run: sensei-clean scan-all   (or)   sensei-clean scan --roots <path>")
    from .waste import human_bytes
    lines = ["Sensei Clean — last status"]
    if state.get("last_full_scan_iso"):
        lines.append(f"  last full scan : {state['last_full_scan_iso']}")
    if state.get("last_total_items") is not None:
        lines.append(f"  files seen     : {state['last_total_items']:,}")
    if state.get("last_total_bytes") is not None:
        lines.append(f"  total size     : {human_bytes(state['last_total_bytes'])}")
    if state.get("last_reclaim_bytes") is not None:
        lines.append(f"  reclaim ready  : {human_bytes(state['last_reclaim_bytes'])} "
                     f"({state.get('last_duplicate_clusters', 0)} duplicate clusters)")
    if state.get("last_run_dir"):
        lines.append(f"  last run dir   : {state['last_run_dir']}")
    if state.get("last_sources"):
        lines.append(f"  sources        : {len(state['last_sources'])}")
        for s in state["last_sources"][:8]:
            lines.append(f"                   - {s}")
        extras = len(state["last_sources"]) - 8
        if extras > 0:
            lines.append(f"                   ... and {extras} more")
    if state.get("last_apply_iso"):
        lines.append("")
        lines.append(f"  last apply     : {state['last_apply_iso']}")
        lines.append(f"                   {state.get('last_apply_applied', 0)} applied, "
                     f"{state.get('last_apply_failed', 0)} failed")
    return "\n".join(lines)
