#!/usr/bin/env python3
"""dashboard_data.py — read-only aggregation for the 3.6 web dashboard.

Pulls from five existing, already-real data sources into one JSON
payload for a new `GET /api/dashboard` endpoint in stt_server.py.
Builds nothing new underneath — every source here already existed
before this file:

  route/model stats  -> observability.summarize()
  task queue          -> headless_daemon.load_jobs()
  approval queue       -> approval_queue.list_all()
  memory/skill browser  -> skill_marketplace.browse_source() +
                           learning_loop.analyze_all()
  chat replay          -> ~/.master_ai_chats/*.chat (most recent N)

No write path anywhere in this module. Any error from one source is
caught and reported per-section (`"error": str(e)`) rather than
failing the whole payload — one broken source shouldn't blank the
dashboard.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

CHATS_DIR = Path.home() / ".master_ai_chats"


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        return {"error": str(e)}


def _route_model_stats(limit: int = 500) -> Dict[str, Any]:
    import observability
    return observability.summarize(limit=limit)


def _task_queue() -> Dict[str, Any]:
    import headless_daemon
    # load_jobs() returns {"version": 1, "jobs": {job_id: {...}}} - the
    # top-level dict is a version wrapper, not the job map itself.
    jobs = headless_daemon.load_jobs().get("jobs", {})
    # Newest first; cap at 50 so the payload stays small.
    ordered = sorted(
        jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True
    )[:50]
    by_status: Dict[str, int] = {}
    for j in jobs.values():
        st = j.get("status", "unknown")
        by_status[st] = by_status.get(st, 0) + 1
    return {"total": len(jobs), "by_status": by_status, "recent": ordered}


def _approval_queue() -> Dict[str, Any]:
    import approval_queue
    all_entries = approval_queue.list_all()
    pending = [e for e in all_entries if e.get("status") == "pending"]
    return {"total": len(all_entries), "pending": pending[:50]}


def _skill_browser() -> Dict[str, Any]:
    import skill_marketplace
    import learning_loop
    skills = skill_marketplace.browse_source("hermes")
    analysis = learning_loop.analyze_all()
    return {
        "skills_found": len(skills),
        "adapted": [s.name for s in skills if getattr(s, "adapted", False)],
        "learning": {
            name: {
                "sessions_analyzed": rep.sessions_analyzed,
                "success_count": rep.success_count,
            }
            for name, rep in analysis.items()
        },
    }


def _chat_replay(limit: int = 20) -> Dict[str, Any]:
    if not CHATS_DIR.exists():
        return {"total": 0, "recent": []}
    files = sorted(
        CHATS_DIR.glob("*.chat"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    recent: List[Dict[str, Any]] = []
    for p in files[:limit]:
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        recent.append({
            "id": p.stem,
            "mtime": p.stat().st_mtime,
            "size_bytes": p.stat().st_size,
            "preview": text[:400],
        })
    return {"total": len(files), "recent": recent}


def build_dashboard(chat_limit: int = 20, stats_limit: int = 500) -> Dict[str, Any]:
    """The one function the HTTP endpoint calls. Read-only, no side effects."""
    return {
        "route_model_stats": _safe(lambda: _route_model_stats(stats_limit)),
        "task_queue": _safe(_task_queue),
        "approval_queue": _safe(_approval_queue),
        "skill_browser": _safe(_skill_browser),
        "chat_replay": _safe(lambda: _chat_replay(chat_limit)),
    }


if __name__ == "__main__":
    print(json.dumps(build_dashboard(), indent=2, default=str))
