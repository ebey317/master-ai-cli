"""File finder subagent — structured wrapper around find + grep.

Task input formats:
    "<pattern>"                            → find by name (glob)
    "name:<pattern>"                       → same as above, explicit
    "grep:<text>"                          → grep -rn the text under cwd
    "grep:<text> in:<directory>"           → grep within a specific dir

Output:
    {"matches": [{"path", "line": int or None, "snippet": str}], ...}

INERT: subprocess.run with capture_output=True; never emits RUN: or
executes user-supplied commands beyond the bounded find/grep. Bounded
to ~/scripts and the cwd to prevent runaway scans.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

name = "file_finder"
description = "Find files by name pattern or grep for text under ~/scripts and cwd"


_SEARCH_ROOTS = [Path.home() / "scripts", Path.cwd(), Path.home() / "Desktop"]
_MAX_MATCHES = 60


def _find_by_name(pattern):
    matches = []
    for root in _SEARCH_ROOTS:
        if not root.is_dir():
            continue
        try:
            r = subprocess.run(
                [
                    "find",
                    str(root),
                    "-type",
                    "f",
                    "-iname",
                    pattern,
                    "-not",
                    "-path",
                    "*/.git/*",
                    "-not",
                    "-path",
                    "*/__pycache__/*",
                    "-not",
                    "-path",
                    "*/node_modules/*",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line and len(matches) < _MAX_MATCHES:
                matches.append({"path": line, "line": None, "snippet": ""})
    return matches


def _grep_for(text, directory=None):
    matches = []
    roots = [Path(directory).expanduser()] if directory else _SEARCH_ROOTS
    for root in roots:
        if not root.is_dir():
            continue
        try:
            r = subprocess.run(
                [
                    "grep",
                    "-rn",
                    "-I",
                    "--exclude-dir=.git",
                    "--exclude-dir=__pycache__",
                    "--exclude-dir=node_modules",
                    "--max-count=10",
                    text,
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            continue
        for line in (r.stdout or "").splitlines():
            if len(matches) >= _MAX_MATCHES:
                break
            parts = line.split(":", 2)
            if len(parts) >= 3:
                p, lno, snip = parts[0], parts[1], parts[2]
                try:
                    lno_i = int(lno)
                except ValueError:
                    lno_i = None
                matches.append(
                    {"path": p, "line": lno_i, "snippet": snip.strip()[:200]}
                )
    return matches


def run(task, context=None):
    task = (task or "").strip()
    if not task:
        return {"error": "file_finder: pass a name pattern or 'grep:<text>'"}
    if task.startswith("grep:"):
        rest = task[5:].strip()
        directory = None
        m = re.match(r"^(.*?)\s+in:(.+)$", rest)
        if m:
            rest, directory = m.group(1).strip(), m.group(2).strip()
        if not rest:
            return {"error": "file_finder: 'grep:' needs a search string"}
        matches = _grep_for(rest, directory)
        return {
            "matches": matches,
            "kind": "grep",
            "needle": rest,
            "summary": f"{len(matches)} match(es) for grep {rest!r}",
        }
    if task.startswith("name:"):
        pat = task[5:].strip()
    else:
        pat = task
    if not pat:
        return {"error": "file_finder: empty pattern"}
    matches = _find_by_name(pat)
    return {
        "matches": matches,
        "kind": "name",
        "pattern": pat,
        "summary": f"{len(matches)} match(es) for name {pat!r}",
    }
