"""
Shared approval gate for Sensei tools.

Design principles (ported from hermes-agent 3e066df):
- Single source of truth for session/permanent grants
- Fail-closed by default when no interactive callback is present
- Pattern-scoped keys (e.g., "shell:rm:recursive", "fs:write:config") so a
  background grant never covers a foreground variant
- Optional explicit callback hook for hosts that want custom prompting
- No tool-specific approval logic; every tool calls `request_approval()`
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ──────────────────────────────────────────────────────────────────────
# Public verdict vocabulary (shared by all tools)
# ──────────────────────────────────────────────────────────────────────

class Verdict(str, Enum):
    ONCE = "once"          # allow this single invocation
    SESSION = "session"    # allow for the rest of this session
    ALWAYS = "always"      # persist to permanent allowlist
    DENY = "deny"          # refuse this invocation
    TIMEOUT = "timeout"    # prompt timed out

# ──────────────────────────────────────────────────────────────────────
# Internal stores (process-wide, thread-safe)
# ──────────────────────────────────────────────────────────────────────

_session_grants: set[str] = set()
_always_grants: set[str] = set()
_stores_lock = threading.RLock()

# Optional explicit callback set by a host (CLI, TUI, API server, …).
# Signature: cb(pattern_key: str, description: str, **kw) -> Verdict
_explicit_callback: Callable[..., Verdict] | None = None
_callback_lock = threading.RLock()


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def set_explicit_callback(cb: Callable[..., Verdict] | None) -> None:
    """
    Install a host-provided approval callback.

    The callback receives (pattern_key, description, **extra) and must return
    a Verdict.  When set, this callback takes precedence over any per-thread
    terminal callback (see `set_thread_callback`).
    """
    global _explicit_callback
    with _callback_lock:
        _explicit_callback = cb


def _get_explicit_callback() -> Callable[..., Verdict] | None:
    with _callback_lock:
        return _explicit_callback


# Per-thread callback (used by interactive terminal UIs).
_thread_callback: threading.local = threading.local()


def set_thread_callback(cb: Callable[..., Verdict] | None) -> None:
    """Install a callback for the current thread only (e.g., terminal prompt)."""
    if cb is None:
        _thread_callback.callback = None  # type: ignore[attr-defined]
    else:
        _thread_callback.callback = cb  # type: ignore[attr-defined]


def _get_thread_callback() -> Callable[..., Verdict] | None:
    return getattr(_thread_callback, "callback", None)


@dataclass(frozen=True)
class ApprovalRequest:
    """Data passed to the approval callback."""
    pattern_key: str
    description: str
    extra: dict[str, Any] = field(default_factory=dict)


def request_approval(
    pattern_key: str,
    description: str,
    *,
    fail_closed_when_no_human: bool = True,
    **extra: Any,
) -> Verdict:
    """
    Ask the user (or policy) whether `pattern_key` is allowed.

    Returns a Verdict.  If no callback is available and
    `fail_closed_when_no_human` is True, returns DENY.  Otherwise returns ONCE
    (legacy permissive behaviour for --yolo style modes).
    """
    # 1. Check permanent allowlist
    if is_approved(pattern_key):
        return Verdict.ALWAYS

    # 2. Check session grants
    with _stores_lock:
        if pattern_key in _session_grants:
            return Verdict.SESSION

    # 3. Ask callbacks (explicit > thread)
    cb = _get_explicit_callback() or _get_thread_callback()
    if cb is None:
        return Verdict.DENY if fail_closed_when_no_human else Verdict.ONCE

    try:
        verdict = cb(pattern_key, description, **extra)
        if not isinstance(verdict, Verdict):
            verdict = Verdict(verdict)
    except Exception:
        # Any callback failure = deny (fail closed)
        return Verdict.DENY

    # 4. Persist grants
    if verdict == Verdict.SESSION:
        with _stores_lock:
            _session_grants.add(pattern_key)
    elif verdict == Verdict.ALWAYS:
        with _stores_lock:
            _always_grants.add(pattern_key)

    return verdict


def is_approved(pattern_key: str) -> bool:
    """Check if a pattern key has a permanent grant."""
    with _stores_lock:
        return pattern_key in _always_grants


def clear_session_grants() -> None:
    """Clear all session-scoped grants (called on session reset)."""
    with _stores_lock:
        _session_grants.clear()


def clear_all_grants() -> None:
    """Clear both session and permanent grants (testing / full reset)."""
    with _stores_lock:
        _session_grants.clear()
        _always_grants.clear()


def list_grants() -> tuple[set[str], set[str]]:
    """Return (session_grants, always_grants) for inspection."""
    with _stores_lock:
        return _session_grants.copy(), _always_grants.copy()


# ──────────────────────────────────────────────────────────────────────
# Cleanup on interpreter exit (helps test isolation)
# ──────────────────────────────────────────────────────────────────────

def _atexit_clear() -> None:
    clear_all_grants()
    set_explicit_callback(None)

atexit.register(_atexit_clear)
