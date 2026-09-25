"""Directive simulator subagent — parses an LLM reply and previews what
the executor WOULD do, without executing anything.

Task input: an LLM reply string (potentially containing RUN:/READ:/
            CREATE:/EDIT:/RUNTERM: directives).
Output:
    {
      "actions": [{"kind", "target", "risk", "requires_confirm"}, ...],
      "count": N,
      "high_risk": [...],     # subset with risk == "high"
      "summary": "<one-line>"
    }

INERT: uses ~/scripts/typed_actions.py's parse_reply() for the parse;
never dispatches. This is the safe preview surface — Pupil can call it
to render "if you approve, here's what will run" before the user OKs.
"""

from __future__ import annotations

import os
import sys

# Make sure typed_actions is importable.
_SCRIPTS = os.path.expanduser("~/scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import typed_actions as _ta

name = "directive_simulator"
description = "Preview what directives in an LLM reply would do, without executing"


def run(task, context=None):
    text = task or ""
    if not isinstance(text, str):
        return {"error": "directive_simulator: task must be a string"}
    actions = _ta.parse_reply(text)
    out = []
    for a in actions:
        out.append(
            {
                "kind": a.kind,
                "target": a.target,
                "risk": a.risk,
                "requires_confirm": a.requires_confirm,
            }
        )
    high = [a for a in out if a["risk"] == "high"]
    return {
        "actions": out,
        "count": len(out),
        "high_risk": high,
        "summary": f"{len(out)} directive(s); {len(high)} high-risk",
    }
