"""Test runner subagent — runs the test_*.py suites in ~/scripts/.

Task input: optional whitespace-separated list of test file stems
            (e.g. "router_golden hooks") OR "all" to run every test_*.py.
            Empty input → list available test files (no execution).
Output:
    {
      "ran":   [{"name", "exit", "passed", "failed", "duration_s"}, ...],
      "available": ["test_router_golden", ...],
      "summary": "<one-line>"
    }

INERT: returns counts + exit codes; never emits RUN: directives. This
subagent IS allowed to invoke python3 on test files (read-only test
runs are part of the contract) but the output never enters the
directive parser.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

name = "test_runner"
description = "Run ~/scripts/test_*.py suites, report pass/fail counts"


SCRIPTS_DIR = Path.home() / "scripts"
_RAN_LINE = re.compile(r"Ran\s+(\d+)\s+tests?", re.I)
_FAIL_LINE = re.compile(r"failures=(\d+)", re.I)
_ERROR_LINE = re.compile(r"errors=(\d+)", re.I)


def _available():
    return sorted(
        p.stem
        for p in SCRIPTS_DIR.glob("test_*.py")
        if p.is_file() and not p.stem.startswith("test__")
    )


def _run_one(stem):
    path = SCRIPTS_DIR / f"{stem}.py"
    if not path.is_file():
        return {"name": stem, "error": "not found"}
    t0 = time.time()
    try:
        r = subprocess.run(
            ["python3", str(path)], capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        return {"name": stem, "error": "timeout (>120s)"}
    elapsed = time.time() - t0
    combined = r.stderr + "\n" + r.stdout
    m_ran = _RAN_LINE.search(combined)
    m_fail = _FAIL_LINE.search(combined)
    m_err = _ERROR_LINE.search(combined)
    ran_n = int(m_ran.group(1)) if m_ran else 0
    fail_n = int(m_fail.group(1)) if m_fail else 0
    err_n = int(m_err.group(1)) if m_err else 0
    return {
        "name": stem,
        "exit": r.returncode,
        "tests": ran_n,
        "failed": fail_n,
        "errors": err_n,
        "passed": max(0, ran_n - fail_n - err_n),
        "duration_s": round(elapsed, 2),
    }


def run(task, context=None):
    task = (task or "").strip()
    available = _available()
    if not task:
        return {
            "available": available,
            "summary": f"{len(available)} test files available; pass names or 'all' to run",
        }
    if task.lower() == "all":
        targets = available
    else:
        targets = [t.strip() for t in task.split() if t.strip()]
    ran = [_run_one(t) for t in targets]
    total_pass = sum(r.get("passed", 0) for r in ran)
    total_fail = sum(r.get("failed", 0) + r.get("errors", 0) for r in ran)
    summary = f"{len(ran)} suite(s) run, {total_pass} pass, {total_fail} fail"
    return {"ran": ran, "available": available, "summary": summary}
