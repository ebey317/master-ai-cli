#!/usr/bin/env python3
"""Workflow cadence helper for Claude Code.

Enforces the 2026-05-18 commit cadence from feedback_work_cadence_commit_push:
  - Per commit  : change + test + commit + tasklist update (manual)
  - Every 5th   : AI_CONTEXT snapshot + git push + memory-save reminder (auto)

Subcommands:
  tick     Read a Claude Code hook JSON payload on stdin, count it iff the
           Bash command was a successful `git commit`, fire the 5th-commit
           batch when counter % 5 == 0.
  reset    Reset the session counter to 0 (call from SessionStart hook).
  status   Print current counter + next-snapshot-in countdown.
  snapshot Write an AI_CONTEXT snapshot now (manual override).
  push     Push the current branch (manual override).

State file:
  /tmp/claude_workflow_counter — single integer, ephemeral per boot.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

COUNTER = Path("/tmp/claude_workflow_counter")
AI_CONTEXT_DIR = Path.home() / "Desktop" / "AI_CONTEXT"
SNAPSHOT_EVERY = 5


def _read_counter() -> int:
    try:
        return int(COUNTER.read_text().strip())
    except (OSError, ValueError):
        return 0


def _write_counter(n: int) -> None:
    COUNTER.write_text(str(n) + "\n")


def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError as e:
        return 127, str(e)


def _git_repo_root() -> str | None:
    # Try cwd, then the canonical project root.
    rc, out = _run(["git", "rev-parse", "--show-toplevel"])
    if rc == 0 and out:
        return out
    fallback = str(Path.home() / "scripts")
    rc, out = _run(["git", "rev-parse", "--show-toplevel"], cwd=fallback)
    if rc == 0 and out:
        return out
    return None


def _snapshot() -> Path:
    AI_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d_%H%M%S")
    path = AI_CONTEXT_DIR / f"context_{ts}.txt"
    repo = _git_repo_root()
    parts = [f"=== AI_CONTEXT SNAPSHOT — {time.strftime('%Y-%m-%d %H:%M:%S')} ==="]
    if repo:
        rc, head = _run(["git", "rev-parse", "HEAD"], cwd=repo)
        rc2, branch = _run(["git", "branch", "--show-current"], cwd=repo)
        rc3, log = _run(
            ["git", "log", "-5", "--oneline", "--no-decorate"], cwd=repo
        )
        rc4, status = _run(["git", "status", "--short"], cwd=repo)
        parts += [
            f"[REPO] {repo}",
            f"[HEAD] {head}",
            f"[BRANCH] {branch}",
            "[LAST 5 COMMITS]",
            log,
            "[WORKING TREE]",
            status or "(clean)",
        ]
    else:
        parts.append("[REPO] (no git repo at cwd)")
    parts.append("")
    parts.append("[OPEN NEXT] (fill in next session)")
    path.write_text("\n".join(parts) + "\n")
    return path


def _push() -> tuple[int, str]:
    repo = _git_repo_root()
    if not repo:
        return 1, "not a git repo"
    rc, branch = _run(["git", "branch", "--show-current"], cwd=repo)
    if rc != 0 or not branch:
        return rc, branch or "no branch"
    return _run(["git", "push", "origin", branch], cwd=repo)


def _fire_fifth_commit_block() -> None:
    msgs = []
    snap = _snapshot()
    msgs.append(f"snapshot {snap}")
    rc, out = _push()
    msgs.append(f"push rc={rc} {out[:200]}")
    msgs.append("memory-save reminder: persist any feedback/project memos from this batch of 5")
    sys.stderr.write("\n[workflow-cadence/5th] " + " | ".join(msgs) + "\n")


def cmd_tick() -> int:
    """Called from PostToolUse hook. Reads JSON payload on stdin."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # No JSON — treat as manual tick.
        payload = {}
    tool_input = payload.get("tool_input") or {}
    command = (tool_input.get("command") or "").strip()
    is_commit = bool(command) and (
        "git commit" in command or command.startswith("git commit")
    )
    if not is_commit:
        return 0
    # Only count if commit actually landed: check tool_response for exit code,
    # else verify HEAD moved by comparing to /tmp/claude_workflow_last_head.
    response = payload.get("tool_response") or {}
    rc_field = response.get("exit_code")
    if rc_field is not None and rc_field != 0:
        return 0
    n = _read_counter() + 1
    _write_counter(n)
    sys.stderr.write(f"[workflow-cadence] commit {n}/{SNAPSHOT_EVERY}\n")
    if n % SNAPSHOT_EVERY == 0:
        _fire_fifth_commit_block()
    return 0


def cmd_reset() -> int:
    _write_counter(0)
    sys.stderr.write("[workflow-cadence] counter reset to 0\n")
    return 0


def cmd_status() -> int:
    n = _read_counter()
    remaining = SNAPSHOT_EVERY - (n % SNAPSHOT_EVERY)
    sys.stdout.write(
        f"commits={n} next_snapshot_in={remaining}\n"
    )
    return 0


def cmd_snapshot() -> int:
    p = _snapshot()
    sys.stdout.write(f"{p}\n")
    return 0


def cmd_push() -> int:
    rc, out = _push()
    sys.stdout.write(out + "\n")
    return rc


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write(__doc__ or "")
        return 2
    sub = argv[1]
    dispatch = {
        "tick": cmd_tick,
        "reset": cmd_reset,
        "status": cmd_status,
        "snapshot": cmd_snapshot,
        "push": cmd_push,
    }
    fn = dispatch.get(sub)
    if not fn:
        sys.stderr.write(f"unknown subcommand: {sub}\n")
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
