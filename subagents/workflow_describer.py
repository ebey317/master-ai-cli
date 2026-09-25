"""Workflow step describer subagent.

Turns recorded browser events into short replay labels. Kept deterministic so
workflow recording works offline and tests do not need a model server.
"""

from __future__ import annotations

import json

name = "workflow_describer"
description = "Describe one recorded browser workflow step in a short sentence"


def _clip(text, limit=120):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _payload(task, context):
    if isinstance(context, dict) and isinstance(context.get("step"), dict):
        return context["step"]
    if isinstance(task, dict):
        return task
    raw = str(task or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {"kind": raw or "step"}


def describe(step):
    kind = str(step.get("kind") or step.get("type") or "").upper()
    target = _clip(
        step.get("target") or step.get("selector") or step.get("url") or "target"
    )
    value = _clip(step.get("value") or step.get("text") or "")
    label = _clip(step.get("label") or step.get("name") or "")
    if kind == "BROWSER_NAV":
        return f"Open {target}"
    if kind == "BROWSER_CLICK":
        return f"Click {label or target}"
    if kind == "BROWSER_DOUBLE_CLICK":
        return f"Open {label or target}"
    if kind == "BROWSER_FILL":
        return f"Fill {label or target}" + (f" with {value}" if value else "")
    if kind == "BROWSER_WAIT":
        return f"Wait {target}"
    if kind == "BROWSER_SCROLL":
        return f"Scroll {target}"
    if kind == "BROWSER_FIND":
        return f"Find {target}"
    return f"Run {kind or 'workflow step'} on {target}"


def run(task, context=None):
    step = _payload(task, context if isinstance(context, dict) else {})
    return {
        "description": describe(step),
        "step_kind": str(step.get("kind") or step.get("type") or ""),
        "subagent": name,
    }
