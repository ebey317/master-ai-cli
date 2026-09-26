"""Session-scoped working directory context management for multi-repo isolation.

Portable reimplementation of the upstream runtime_cwd design: a contextvar-backed
session CWD that can be temporarily bound for RPC/skill-discovery operations,
with a token-based reset API. This avoids polluting process-global os.getcwd()
while giving background threads the session's logical workspace.
"""
from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_SESSION_CWD: contextvars.ContextVar[str] = contextvars.ContextVar("_session_cwd", default="")

def set_session_cwd(path: str | os.PathLike[str]) -> contextvars.Token[str]:
    """Bind the session's logical CWD for the current context; returns a reset token."""
    return _SESSION_CWD.set(str(path))

def reset_session_cwd(token: contextvars.Token[str]) -> None:
    """Restore the logical CWD that was active before the matching set_session_cwd."""
    _SESSION_CWD.reset(token)

def get_session_cwd() -> str:
    """Current session-scoped CWD (empty if not bound)."""
    return _SESSION_CWD.get()

@contextmanager
def session_cwd_scope(path: str | os.PathLike[str]) -> Iterator[None]:
    """Context manager that binds session CWD for the block and restores on exit."""
    token = set_session_cwd(path)
    try:
        yield
    finally:
        reset_session_cwd(token)

def resolve_effective_cwd(fallback: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the effective CWD: session CWD > process CWD > fallback > home."""
    session = get_session_cwd()
    if session:
        return Path(session).expanduser().resolve()
    proc = Path.cwd()
    if proc:
        return proc
    if fallback:
        return Path(fallback).expanduser().resolve()
    return Path.home()
