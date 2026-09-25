"""Code reviewer subagent — syntax + style + smell checks on .py / .sh.

Task input: whitespace-separated list of file paths.
Output:
    {
      "issues": [{"path", "kind", "msg"}, ...],
      "files_reviewed": N,
      "summary": "<one-line>"
    }

Never returns RUN: / EDIT: / CREATE: directives — pure inert structured
data. The executor will NOT parse this for directives.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

name = "code_reviewer"
description = "Review .py/.sh files for syntax errors, TODO/FIXME, and basic smells"


_SMELL_PATTERNS = [
    (re.compile(r"\bprint\s*\("), "style", "uses print() — consider logging"),
    (re.compile(r"^\s*#\s*TODO\b", re.M), "todo", "TODO marker found"),
    (re.compile(r"^\s*#\s*FIXME\b", re.M), "todo", "FIXME marker found"),
    (re.compile(r"\bexcept\s*:\s*$", re.M), "style", "bare except clause"),
    (re.compile(r"\beval\s*\("), "smell", "eval() — consider safer alternatives"),
    (re.compile(r"\bexec\s*\("), "smell", "exec() — consider safer alternatives"),
]


def _check_py(path: str) -> list:
    issues = []
    try:
        r = subprocess.run(
            ["python3", "-m", "py_compile", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return [{"path": path, "kind": "tool", "msg": "py_compile unavailable"}]
    if r.returncode != 0:
        err_src = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
        last_line = (err_src.splitlines() or [""])[-1][:200]
        issues.append({"path": path, "kind": "syntax", "msg": last_line})
        # Syntax error means the rest of the smell scan is unreliable.
        return issues
    try:
        text = Path(path).read_text(errors="replace")
    except Exception:
        return issues
    for pat, kind, msg in _SMELL_PATTERNS:
        if pat.search(text):
            issues.append({"path": path, "kind": kind, "msg": msg})
    return issues


def _check_sh(path: str) -> list:
    issues = []
    try:
        r = subprocess.run(
            ["bash", "-n", path], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return [{"path": path, "kind": "tool", "msg": "bash -n unavailable"}]
    if r.returncode != 0:
        err_src = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
        first_line = (err_src.splitlines() or [""])[0][:200]
        issues.append({"path": path, "kind": "syntax", "msg": first_line})
    return issues


def run(task, context=None):
    task = (task or "").strip()
    if not task:
        return {"error": "code_reviewer: pass at least one file path"}
    paths = [p for p in task.split() if p]
    issues = []
    reviewed = 0
    for p in paths:
        if not Path(p).is_file():
            issues.append({"path": p, "kind": "missing", "msg": "file not found"})
            continue
        if p.endswith(".py"):
            issues.extend(_check_py(p))
            reviewed += 1
        elif p.endswith((".sh", ".bash", ".zsh")):
            issues.extend(_check_sh(p))
            reviewed += 1
        else:
            issues.append(
                {
                    "path": p,
                    "kind": "skip",
                    "msg": "unsupported extension (.py/.sh only)",
                }
            )
    summary = (
        f"{reviewed} file(s) reviewed; "
        f"{len([i for i in issues if i['kind'] == 'syntax'])} syntax, "
        f"{len([i for i in issues if i['kind'] == 'todo'])} TODO/FIXME, "
        f"{len([i for i in issues if i['kind'] in ('style', 'smell')])} other"
    )
    return {"issues": issues, "files_reviewed": reviewed, "summary": summary}
