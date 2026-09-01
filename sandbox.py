"""Shared least-privilege sandbox wrapper for shell execution.

2026-09-01: extracted out of master_ai.py so skill_runtime.py recipes can
use the exact same sandboxing as master_ai's own RUN dispatch (run_command)
instead of calling subprocess.run() directly and bypassing it entirely.
Standalone, stdlib-only, no master_ai import -- master_ai.py already
imports skill_runtime.py, so skill_runtime importing master_ai back would
be circular. This module is the shared dependency both sides import
independently.

Public API:
    build_sandbox_argv(exec_cmd) -> list   -- wrap an argv list
    run_sandboxed(argv, timeout=60, **kw)   -- subprocess.run() through it
"""

from __future__ import annotations

import subprocess

_SANDBOX_HIDE_SECRETS_SH = (
    'for raw in "$HOME/.ssh" "$HOME/.aws" "$HOME/.master_ai_keys"; do '
    'p=$(readlink -f "$raw" 2>/dev/null) || p="$raw"; '
    'if [ -d "$p" ]; then mount -t tmpfs -o size=1k,mode=000 tmpfs "$p" 2>/dev/null; '
    'elif [ -e "$p" ]; then mount --bind /dev/null "$p" 2>/dev/null; fi; '
    'done; exec "$@"'
)


def build_sandbox_argv(exec_cmd):
    """Wrap exec_cmd (already a list -- never shell=True) in a least-
    privilege sandbox before it reaches subprocess.run.

    2026-09-01 (Phase 1.2): built for master_ai.py's run_command(), then
    extracted here the same day once skill_runtime.py recipes turned out
    to bypass it entirely by calling subprocess.run() directly. Deliberately
    deviates from the literal wrapper in Elijah's own ROADMAP.md in two
    ways, both found by testing the actual commands on this machine before
    writing this, not by implementing the snippet on faith:

    1. Dropped `-n` (new network namespace) -- breaks networking entirely
       with no veth/NAT set up. ~/.master_ai_audit.log shows curl/wget/
       apt-cache/dpkg in real daily use (weather checks, GitHub API
       lookups, package checks) -- `-n` as written would have silently
       broken most of what RUN is actually used for.
    2. Dropped `prlimit --nproc=` for process-count limiting and use
       `systemd-run --user --scope -p TasksMax=` instead. RLIMIT_NPROC
       (what `prlimit --nproc` sets) is accounted per REAL UID **system-
       wide**, not per process subtree (see `man getrlimit`) -- tested
       live: `prlimit --nproc=200 -- unshare ...` failed outright with
       "fork failed: Resource temporarily unavailable" even though only
       ~108 processes existed for this user, and it only started working
       north of 500. That's not fork-bomb containment, that's a landmine
       that could break a normal multi-CLI desktop session. `systemd-run
       --user --scope -p TasksMax=200` uses the cgroup v2 pids controller
       instead, which genuinely scopes the limit to just this command's
       own process subtree -- verified live: a real fork bomb under
       TasksMax=50 only moved the system-wide process count from ~108 to
       ~161 (bomb contained inside its own cgroup), not an unbounded climb.

    `prlimit --nofile=`/`--as=` stay as plain prlimit -- unlike NPROC,
    RLIMIT_NOFILE and RLIMIT_AS are per-process, not pooled across the
    user's other processes, so they carry none of the NPROC risk. `--as=`
    (virtual memory) is used instead of the roadmap snippet's `--data=`
    (brk-heap) -- --data doesn't bound modern glibc/mmap allocators, --as
    actually does; MemoryMax on the same cgroup scope backs it up with a
    hard kill if a command tries to blow past it anyway.

    unshare -U -m -p --map-root-user is fully unprivileged on this box
    (verified: /proc/sys/kernel/unprivileged_userns_clone=1) and gives
    real mount+PID isolation without touching networking -- needed so the
    secret-path bind-mounts below only affect this command, not the rest
    of the system.

    The three secret paths are hidden by bind-mounting over their
    *resolved* real path (readlink -f) so a symlink (e.g. ~/.master_ai_keys
    -> ~/Desktop/Projects/keychain/master_ai_keys) is hidden at its real
    location, not just the symlink stub. exec_cmd is passed as literal
    argv elements after the wrapper script via `"$@"` -- never string-
    interpolated -- so wrapping introduces no new shell-injection surface
    versus running exec_cmd directly.
    """
    return [
        "systemd-run", "--user", "--scope", "--quiet",
        "-p", "TasksMax=200", "-p", "MemoryMax=1G",
        "--",
        "unshare", "-U", "-m", "-p", "--mount-proc", "--map-root-user", "-f",
        "--",
        "prlimit", "--nofile=512", "--as=1073741824",
        "--",
        "bash", "-c", _SANDBOX_HIDE_SECRETS_SH, "sandbox-wrapper",
        *exec_cmd,
    ]


def run_sandboxed(argv, *, timeout: int = 60, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run(argv, ...) through the sandbox -- drop-in replacement
    for a bare subprocess.run(argv, capture_output=True, text=True,
    timeout=N) call. Same signature shape, same return type, so callers'
    existing result.stdout/.stderr/.returncode handling and
    `except subprocess.TimeoutExpired` blocks need no changes."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(build_sandbox_argv(argv), timeout=timeout, **kwargs)
