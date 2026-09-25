"""Context inspector subagent — show what auto-context the slicer WOULD
inject for a given user query, without dispatching the model.

Task input: a user-prompt string (same shape the orchestrator would
            receive on a turn).
Output:
    {
      "inject_chars":            int,
      "whole_file_requested":    bool,
      "big_file_no_symbol_match": [str path, ...],
      "sliced": [{"path", "symbol", "start", "end"}],
      "summary": "<one-line>"
    }

INERT: calls master_ai.auto_inject_context(query, enabled=True) and
returns the meta dict. Reads filesystem (file injection) but does NOT
run any tool, hit any LLM, or emit a directive.
"""

from __future__ import annotations

import os
import sys

_SCRIPTS = os.path.expanduser("~/scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

name = "context_inspector"
description = "Preview the auto-context slicer's output for a query (no model call)"


def run(task, context=None):
    text = (task or "").strip()
    if not text:
        return {"error": "context_inspector: pass a user prompt"}
    try:
        os.environ.setdefault("SENSEI_TUI", "0")
        import master_ai  # noqa: E402
    except Exception as e:
        return {"error": f"master_ai import failed: {e}"}
    try:
        injected, meta = master_ai.auto_inject_context(text, enabled=True)
    except Exception as e:
        return {"error": f"auto_inject_context failed: {e}"}
    sliced_out = []
    for entry in meta.get("sliced", []):
        if isinstance(entry, tuple) and len(entry) == 4:
            path, sym, start, end = entry
            sliced_out.append(
                {
                    "path": str(path),
                    "symbol": str(sym),
                    "start": int(start),
                    "end": int(end),
                }
            )
    return {
        "inject_chars": len(injected or ""),
        "whole_file_requested": bool(meta.get("whole_file_requested")),
        "big_file_no_symbol_match": [
            str(p) for p in meta.get("big_file_no_symbol_match", [])
        ],
        "sliced": sliced_out,
        "summary": (
            f"{len(sliced_out)} slice(s); "
            f"{len(meta.get('big_file_no_symbol_match', []))} big-file-no-symbol; "
            f"{len(injected or '')} chars"
        ),
    }
