"""Spend reporter subagent — efficiency rollup over harvest cache hits,
route distribution, blocked actions, and recent execution outcomes.

Task input: optional limit (integer string, e.g. "1000") for how many
            recent events to scan. Empty → 500.
Output:
    {
      "harvest":    {"hits", "records", "ratio_pct"},
      "by_route":   {...},
      "by_model":   {...},
      "executions": {"ok", "fail", "success_pct"},
      "blocked":    {"total", "by_kind"},
      "summary":    "<one-line>"
    }

INERT: reads JSONL files via ~/scripts/observability.py's summarize();
never dispatches commands or model calls. Pure read.
"""

from __future__ import annotations

import os
import sys

_SCRIPTS = os.path.expanduser("~/scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import observability as _obs

name = "spend_reporter"
description = (
    "Efficiency rollup: harvest hits, route distribution, execution success rate"
)


def run(task, context=None):
    limit = 500
    s = (task or "").strip()
    if s:
        try:
            limit = max(10, min(int(s), 50000))
        except ValueError:
            pass
    summary = _obs.summarize(limit=limit)
    harvest = summary.get("harvest", {})
    hits = int(harvest.get("hits", 0))
    records = int(harvest.get("records", 0))
    total_model_calls = summary.get("model_calls", 0) + hits
    ratio = (hits * 100 // total_model_calls) if total_model_calls else 0
    ex = summary.get("executions", {})
    ok = int(ex.get("ok", 0))
    fail = int(ex.get("fail", 0))
    total_ex = ok + fail
    succ = (ok * 100 // total_ex) if total_ex else 0
    return {
        "harvest": {"hits": hits, "records": records, "ratio_pct": ratio},
        "by_route": summary.get("by_route", {}),
        "by_model": summary.get("by_model", {}),
        "executions": {"ok": ok, "fail": fail, "success_pct": succ},
        "blocked": summary.get("blocked", {}),
        "summary": (
            f"harvest ratio: {ratio}%  ·  "
            f"execution success: {succ}%  ·  "
            f"{summary.get('route_decisions', 0)} route decisions in last {limit}"
        ),
    }
