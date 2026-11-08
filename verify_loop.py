"""Sensei verify loop.

This module provides harness-style guardrails that hook into Sensei's existing
post_create/post_edit lifecycle. It is gated by the environment variable
MASTER_AI_VERIFY_LOOP=1 (default OFF) so it can be measured before becoming the
product default.

What it adds:
- Auto-verify: after a file edit/create lands, detect the project's pytest
  command and run it; feed the last ~15 lines back to the model as a tool result.
- Fence-tolerant EDIT bodies: strips ```lang fences from inside <<<FIND>>>/
  <<<REPLACE>>> blocks before dispatch so models can safely fence code snippets.
- Edit-failure reason collection for the post-* hook path.

The module intentionally does not touch approval/safety rules; it only observes
and reports. It also does not run any shell command except the detected pytest.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_VERIFY_ROUNDS = int(os.environ.get("MASTER_AI_VERIFY_MAX_ROUNDS", "5"))
TAIL_LINES = int(os.environ.get("MASTER_AI_VERIFY_TAIL_LINES", "15"))
VERIFY_TIMEOUT = float(os.environ.get("MASTER_AI_VERIFY_TIMEOUT", "120.0"))


def enabled() -> bool:
    """True when the user has opted into the Sensei verify loop."""
    return os.environ.get("MASTER_AI_VERIFY_LOOP", "").strip() == "1"


def detect_test_command(cwd: str | Path | None = None) -> list[str] | None:
    """Return a pytest argv if the directory looks like a Python project.

    Only detects `pytest`/`python3 -m pytest` when `tests/` exists or there
    are `test_*.py` files at the root. Returns None otherwise.
    """
    root = Path(cwd or Path.cwd()).resolve()
    has_tests_dir = (root / "tests").is_dir()
    has_test_files = any(
        p.name.startswith("test_") and p.suffix == ".py"
        for p in root.iterdir()
        if p.is_file()
    )
    if not (has_tests_dir or has_test_files):
        return None
    # Prefer the active interpreter so the loop runs with the same Python.
    return [sys.executable, "-m", "pytest", "-q"]


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])


def run_tests(cwd: str | Path | None = None) -> tuple[int, str]:
    """Run the detected test command and return (exit_code, tail)."""
    root = Path(cwd or Path.cwd()).resolve()
    argv = detect_test_command(root)
    if argv is None:
        return 0, "[verify loop] no pytest project detected; skipping auto-verify"
    try:
        result = subprocess.run(
            argv,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT,
        )
        tail = _tail(result.stdout + "\n" + result.stderr)
        return result.returncode, tail
    except subprocess.TimeoutExpired:
        return 124, f"[verify loop] pytest timed out after {VERIFY_TIMEOUT}s"
    except Exception as e:
        return 1, f"[verify loop] pytest failed to run: {e}"


def _strip_fenced_body(text: str) -> str:
    """Remove a surrounding ```lang ... ``` fence pair if present."""
    # Only touch text that really is fenced; never alter indentation/whitespace
    # of ordinary EDIT bodies (find text must still match the file exactly).
    m = re.match(r"\s*```[A-Za-z0-9_-]*[ \t]*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def strip_edit_fences(find_text: str, replace_text: str) -> tuple[str, str]:
    """Return (find_text, replace_text) with any inner code fences stripped."""
    return _strip_fenced_body(find_text), _strip_fenced_body(replace_text)


def verify_feedback(exit_code: int, tail: str) -> str:
    """Format pytest output as a user-role message for the model."""
    status = "PASS" if exit_code == 0 else "FAIL"
    return f"[AUTO-VERIFY RESULT] {status}\n{tail}\n\nIf tests passed, summarize the result. If they failed, fix the failure before continuing."


def post_edit_hook(
    history: list[dict[str, str]],
    cwd: str | Path | None = None,
) -> None:
    """Hook after a Sensei edit/create lands when MASTER_AI_VERIFY_LOOP=1.

    Detects the test command, runs it, and appends the tail to history.
    Caps the number of auto-verify rounds by counting existing
    [AUTO-VERIFY RESULT] messages in history.
    """
    if not enabled():
        return
    already = sum(
        1
        for msg in history
        if msg.get("role") == "user"
        and "[AUTO-VERIFY RESULT]" in (msg.get("content") or "")
    )
    if already >= MAX_VERIFY_ROUNDS:
        history.append(
            {
                "role": "user",
                "content": (
                    "[AUTO-VERIFY RESULT] SKIPPED\n"
                    f"Reached the auto-verify round cap ({MAX_VERIFY_ROUNDS}). "
                    "Continue without running more tests."
                ),
            }
        )
        return
    exit_code, tail = run_tests(cwd)
    history.append({"role": "user", "content": verify_feedback(exit_code, tail)})


def normalize_edit_op(
    filepath: str,
    find_text: str,
    replace_text: str,
) -> tuple[str, str]:
    """Strip fences from an EDIT operation before it is applied.

    Flag OFF means the product behaves exactly as before: no change."""
    if not enabled():
        return find_text, replace_text
    return strip_edit_fences(find_text, replace_text)


def explain_create_failure(reason: str, filepath: str) -> str:
    """Return a concise message explaining why a CREATE failed."""
    return f"CREATE failed for {filepath}: {reason}"


def explain_edit_failure(reason: str, filepath: str) -> str:
    """Return a concise message explaining why an EDIT failed."""
    return f"EDIT failed for {filepath}: {reason}"


def api_summary() -> dict[str, Any]:
    """Return a JSON-serializable summary of the current verify-loop config."""
    return {
        "enabled": enabled(),
        "max_rounds": MAX_VERIFY_ROUNDS,
        "tail_lines": TAIL_LINES,
        "timeout": VERIFY_TIMEOUT,
        "cwd": str(Path.cwd()),
        "detected_command": detect_test_command(),
    }
