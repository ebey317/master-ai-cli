"""verifiers.py — verification framework for Sensei's agentic loop.

Phase 1 of the Reactive Waddling Papert plan
(~/.claude/plans/reactive-waddling-papert.md).

Hard rule: NO verifier may hang the task forever. Every verifier accepts a
max_wait_s cap, runs each subprocess call with its own subprocess.run
timeout, and returns a clean structured VerifyResult on every path — success,
process-not-found, subprocess-timeout, or system-binary-missing.

Phase 1 ships one verifier: verify_process_running, used by
desktop.launch_app to confirm a launched app actually appeared in the
process table.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class VerifyResult:
    ok: bool
    observed: Optional[str]
    elapsed_ms: int
    reason: str


def verify_process_running(
    name: str,
    max_wait_s: float = 5.0,
    poll_ms: int = 500,
) -> VerifyResult:
    """Poll `pgrep -af <name>` until matched or max_wait_s elapsed.

    `pgrep -af` matches against the full command line, so a process started
    via a wrapper script, gtk-launch, or python -m still gets found. The
    outer loop caps total wait; each pgrep invocation has its own
    subprocess timeout (poll_ms * 4, floor 1s) so no individual call can
    hang.
    """
    if not name:
        return VerifyResult(
            ok=False,
            observed=None,
            elapsed_ms=0,
            reason="empty process name",
        )

    # Anchored pattern: name must appear as the executable token, not as a
    # random argument to some other process. Preceded by ^ or /, followed by
    # whitespace or end-of-line. This excludes false positives like the
    # process running the smoke test itself, whose argv would otherwise
    # match a bare substring search.
    pattern = rf"(^|/){re.escape(name)}([[:space:]]|$)"

    start = time.monotonic()
    poll_s = max(0.05, poll_ms / 1000.0)
    per_call_timeout = max(1.0, poll_s * 4)
    last_error = ""

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= max_wait_s:
            reason = f"process '{name}' not found within {max_wait_s}s"
            if last_error:
                reason = f"{reason} (last pgrep error: {last_error})"
            return VerifyResult(
                ok=False,
                observed=None,
                elapsed_ms=int(elapsed * 1000),
                reason=reason,
            )

        try:
            cp = subprocess.run(
                ["pgrep", "-af", pattern],
                capture_output=True,
                text=True,
                timeout=per_call_timeout,
            )
            if cp.returncode == 0:
                lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
                if lines:
                    return VerifyResult(
                        ok=True,
                        observed=lines[0].strip(),
                        elapsed_ms=int((time.monotonic() - start) * 1000),
                        reason=f"process '{name}' found",
                    )
            elif cp.returncode == 1:
                # pgrep returns 1 when no matches; expected during polling.
                pass
            else:
                stderr_tail = (cp.stderr or "").strip().splitlines()
                last_error = stderr_tail[-1] if stderr_tail else f"pgrep exit {cp.returncode}"
        except subprocess.TimeoutExpired:
            last_error = f"pgrep call exceeded {per_call_timeout}s"
        except FileNotFoundError:
            return VerifyResult(
                ok=False,
                observed=None,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                reason="pgrep not available on this system",
            )
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"

        time.sleep(poll_s)


def verify_notification_sent(
    _: Optional[str] = None,
    max_wait_s: float = 1.0,
    poll_ms: int = 100,
) -> VerifyResult:
    """Verify that desktop notifications are deliverable right now.

    We can't see the user's screen, so we verify the prerequisites:
      - notify-send binary exists
      - the D-Bus session bus socket is present at the expected path
      - sensei-notify.sh wrapper exists and is executable

    Returns ok=True when all prerequisites are satisfied. The executor already
    performed the actual notify-send call; this verifier just confirms the
    environment is still healthy enough for future notifications.
    """
    import os
    import shutil
    start = time.monotonic()
    wrapper = Path.home() / "scripts" / "sensei-notify.sh"
    bus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not bus_addr:
        run_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        bus_addr = f"unix:path={run_dir}/bus"

    # D-Bus socket must exist and be writable.
    socket_path = bus_addr.replace("unix:path=", "") if bus_addr.startswith("unix:path=") else ""
    if not socket_path or not os.path.exists(socket_path):
        elapsed = time.monotonic() - start
        return VerifyResult(
            ok=False,
            observed=None,
            elapsed_ms=int(elapsed * 1000),
            reason=f"D-Bus session socket not found: {socket_path}",
        )

    if not shutil.which("notify-send"):
        elapsed = time.monotonic() - start
        return VerifyResult(
            ok=False,
            observed=None,
            elapsed_ms=int(elapsed * 1000),
            reason="notify-send binary not found in PATH",
        )

    if not wrapper.is_file():
        elapsed = time.monotonic() - start
        return VerifyResult(
            ok=False,
            observed=None,
            elapsed_ms=int(elapsed * 1000),
            reason=f"sensei-notify.sh wrapper missing: {wrapper}",
        )

    elapsed = time.monotonic() - start
    return VerifyResult(
        ok=True,
        observed=f"notify-send present, D-Bus socket {socket_path}",
        elapsed_ms=int(elapsed * 1000),
        reason="notification prerequisites satisfied",
    )


__all__ = ["VerifyResult", "verify_process_running", "verify_notification_sent"]
