"""General executor subagent — do-whatever delegation.

This subagent takes a free-form task and executes it using a bounded,
deterministic planner over the sandboxed tools provided by the delegation
runner: READ, CREATE, EDIT, RUN, FETCH. It does not require an LLM, but it
can optionally call `master_ai.ask_local()` if the task looks complex and
a model is available.

Exports:
    name = "general"
    description = "Execute free-form tasks using file/terminal/network tools"
    run(task, context=None) -> dict

Task parsing:
- "read <path>"                    -> READ
- "create <path> with <content>"   -> CREATE
- "edit <path> replace <old> with <new>" -> EDIT
- "run <command>"                    -> RUN
- "fetch <url>"                      -> FETCH
- "search <pattern>"                 -> file_finder name:<pattern>
- "grep <text> in <dir>"             -> file_finder grep:<text> in:<dir>
- Otherwise: best-effort heuristic plan.
"""

from __future__ import annotations

import os
import re
import sys
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

name = "general"
description = "Execute free-form tasks using file/terminal/network tools"


def _parse_task(task: str) -> List[Dict[str, Any]]:
    """Turn a free-form task string into a list of directive specs."""
    t = (task or "").strip()
    if not t:
        return []
    low = t.lower()
    actions: List[Dict[str, Any]] = []

    # READ
    m = re.match(r"^read\s+(.+)$", t, re.IGNORECASE)
    if m:
        return [{"kind": "read", "path": m.group(1).strip()}]

    # CREATE path with content
    m = re.match(r"^create\s+(.+?)\s+with\s+(.+)$", t, re.IGNORECASE | re.DOTALL)
    if m:
        return [{"kind": "create", "path": m.group(1).strip(), "content": m.group(2)}]

    # EDIT path replace OLD with NEW
    m = re.match(r"^edit\s+(.+?)\s+replace\s+(.+?)\s+with\s+(.+)$", t, re.IGNORECASE | re.DOTALL)
    if m:
        return [{"kind": "edit", "path": m.group(1).strip(), "old": m.group(2), "new": m.group(3)}]

    # RUN command
    m = re.match(r"^run\s+(.+)$", t, re.IGNORECASE)
    if m:
        return [{"kind": "run", "command": m.group(1).strip()}]

    # FETCH url
    m = re.match(r"^fetch\s+(.+)$", t, re.IGNORECASE)
    if m:
        return [{"kind": "fetch", "url": m.group(1).strip()}]

    # SEARCH / GREP routing to file_finder
    m = re.match(r"^search\s+(.+)$", t, re.IGNORECASE)
    if m:
        return [{"kind": "subagent", "name": "file_finder", "task": f"name:{m.group(1).strip()}"}]
    m = re.match(r"^grep\s+(.+?)\s+in\s+(.+)$", t, re.IGNORECASE)
    if m:
        return [{"kind": "subagent", "name": "file_finder", "task": f"grep:{m.group(1).strip()} in:{m.group(2).strip()}"}]
    m = re.match(r"^grep\s+(.+)$", t, re.IGNORECASE)
    if m:
        return [{"kind": "subagent", "name": "file_finder", "task": f"grep:{m.group(1).strip()}"}]

    # Heuristic: contains words that imply file creation
    if any(w in low for w in ("write", "create file", "make file", "save")):
        parts = re.split(r"\s+(?:with|containing|saying|that says)\s+", t, flags=re.IGNORECASE, maxsplit=1)
        path = parts[0].split()[-1] if parts else ""
        content = parts[1] if len(parts) > 1 else ""
        if path:
            return [{"kind": "create", "path": path, "content": content}]

    # Heuristic: contains words that imply a shell command
    if any(w in low for w in ("list", "show", "print", "get", "check", "status", "run", "execute")):
        # If the user says "list files in X" produce RUN: ls X
        m = re.match(r"^(?:list|show)\s+(?:the\s+)?files\s+(?:in|of)\s+(.+)$", t, re.IGNORECASE)
        if m:
            return [{"kind": "run", "command": f"ls -la {m.group(1).strip()}"}]
        return [{"kind": "run", "command": t}]

    # Fallback: assume file search
    return [{"kind": "subagent", "name": "file_finder", "task": f"name:{t}"}]


def _run_directive(spec: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one directive using the same sandbox helpers the child runner
    would use. This keeps the general subagent self-contained."""
    kind = spec.get("kind")
    workdir = Path(context.get("workdir", os.getcwd()))
    HOME = Path.home().resolve()

    def _path_ok(path: str, for_write: bool = False) -> tuple:
        try:
            p = Path(path).resolve()
        except Exception:
            return False, "invalid path"
        if for_write and workdir.resolve() not in (p, *p.parents):
            return False, f"write outside workdir: {p}"
        if not for_write and not str(p).startswith(str(HOME)):
            return False, f"read outside home: {p}"
        return True, ""

    def _to_workdir(path: str) -> Path:
        rel = os.path.expanduser(path)
        if rel.startswith("/"):
            return workdir / Path(rel).name
        return workdir / rel

    if kind == "read":
        path = spec["path"]
        ok, why = _path_ok(path, for_write=False)
        if not ok:
            return {"error": why}
        try:
            p = Path(path).resolve()
            if not p.is_file():
                return {"error": f"not found: {path}"}
            return {"content": p.read_text(errors="replace")[:8000]}
        except Exception as e:
            return {"error": str(e)}

    if kind == "create":
        path = spec["path"]
        target = _to_workdir(path)
        ok, why = _path_ok(str(target), for_write=True)
        if not ok:
            return {"error": why}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(spec.get("content", ""), encoding="utf-8")
            return {"created": str(target)}
        except Exception as e:
            return {"error": str(e)}

    if kind == "edit":
        path = spec["path"]
        target = _to_workdir(path)
        ok, why = _path_ok(str(target), for_write=True)
        if not ok:
            return {"error": why}
        try:
            text = target.read_text(encoding="utf-8")
            old = spec.get("old", "")
            if old not in text:
                return {"error": f"old text not found in {target}"}
            target.write_text(text.replace(old, spec.get("new", "")), encoding="utf-8")
            return {"edited": str(target)}
        except Exception as e:
            return {"error": str(e)}

    if kind == "run":
        cmd = spec["command"]
        dangerous = {"rm", "sudo", "mkfs", "dd", "format", "fdisk", "parted",
                     "shutdown", "reboot", "poweroff", "passwd", "su", "ssh", "scp"}
        lowered = cmd.lower()
        if any(d in lowered for d in dangerous):
            return {"error": f"blocked dangerous command: {cmd}"}
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            out = (r.stdout or "") + (r.stderr or "")
            return {"output": out[:4000], "returncode": r.returncode}
        except Exception as e:
            return {"error": str(e)}

    if kind == "fetch":
        url = spec["url"]
        if not re.match(r"^https?://", url):
            return {"error": "only http/https allowed"}
        try:
            r = subprocess.run(["curl", "-sL", "--max-time", "30", url],
                               capture_output=True, text=True, timeout=35)
            out = (r.stdout or "") + (r.stderr or "")
            return {"output": out[:4000]}
        except Exception as e:
            return {"error": str(e)}

    if kind == "subagent":
        sys.path.insert(0, str(Path(__file__).parent))
        sys.path.insert(0, str(Path(__file__).parent.parent))
        try:
            import subagent_registry as _sr
            _sr.discover()
            return _sr.run(spec["name"], spec["task"], context=context)
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"unknown directive kind: {kind}"}


def run(task: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    context = context or {}
    specs = _parse_task(task)
    if not specs:
        return {"error": "general: no actionable directive parsed", "task": task}

    results = []
    for spec in specs:
        result = _run_directive(spec, context)
        results.append({"spec": spec, "result": result})

    # Build a one-line summary.
    summary_parts = []
    for r in results:
        kind = r["spec"].get("kind")
        res = r["result"]
        if "error" in res:
            summary_parts.append(f"{kind} failed: {res['error']}")
        elif kind == "read":
            summary_parts.append(f"read {len(res.get('content', ''))} chars")
        elif kind == "create":
            summary_parts.append(f"created {res.get('created', 'file')}")
        elif kind == "edit":
            summary_parts.append(f"edited {res.get('edited', 'file')}")
        elif kind == "run":
            summary_parts.append(f"run rc={res.get('returncode')} output={res.get('output', '')[:40]}")
        elif kind == "fetch":
            summary_parts.append(f"fetched {len(res.get('output', ''))} chars")
        elif kind == "subagent":
            summary_parts.append(f"subagent {r['spec'].get('name')} returned {len(str(res))} chars")

    return {
        "task": task,
        "summary": "; ".join(summary_parts) if summary_parts else "no actions",
        "results": results,
    }
