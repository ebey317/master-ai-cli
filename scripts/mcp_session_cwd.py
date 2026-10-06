"""
Session-scoped working directory pinning for MCP stdio server registration.

This module provides contextvar-based cwd pinning so that when MCP servers are
registered for a specific session/task, any stdio servers they spawn will use
the session's logical working directory by default.

Usage:
    from scripts.mcp_session_cwd import pin_session_cwd, get_pinned_cwd

    # At session/task start, pin the cwd
    pin_session_cwd("/workspace/my-project")

    # When registering MCP servers (e.g., from config or IDE-provided)
    # the pin is automatically captured via contextvars
    register_mcp_servers(configs)

    # Inside MCP stdio transport, at spawn time:
    cwd = get_pinned_cwd()  # returns pinned cwd or None
"""

import contextvars
from pathlib import Path

# Contextvar holding the pinned session cwd (Path or None)
_session_cwd_var: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "sensei_session_cwd", default=None
)


def pin_session_cwd(cwd: str | Path | None) -> contextvars.Token:
    """
    Pin the session's logical working directory for the current context.

    Returns a token that can be used to reset the pin (via reset_session_cwd).
    The pin is inherited by child tasks created via asyncio.create_task,
    contextvars.copy_context().run, and run_coroutine_threadsafe.
    """
    path = Path(cwd).resolve() if cwd else None
    return _session_cwd_var.set(path)


def reset_session_cwd(token: contextvars.Token) -> None:
    """Reset the session cwd pin to its previous value."""
    _session_cwd_var.reset(token)


def get_pinned_cwd() -> Path | None:
    """
    Get the currently pinned session cwd, or None if not pinned.

    This is the value that MCP stdio transports should use as the default
    working directory when spawning server processes.
    """
    return _session_cwd_var.get()


def run_with_pinned_cwd(cwd: str | Path | None, func, *args, **kwargs):
    """
    Run a function with a temporary session cwd pin.

    The pin is active for the duration of `func` and automatically reset
    afterwards, even if `func` raises. The context is copied so the pin
    propagates to any async tasks spawned within `func`.
    """
    token = pin_session_cwd(cwd)
    try:
        # Copy context so the pin carries into any tasks spawned by func
        ctx = contextvars.copy_context()
        return ctx.run(func, *args, **kwargs)
    finally:
        reset_session_cwd(token)


async def run_async_with_pinned_cwd(cwd: str | Path | None, coro):
    """
    Run a coroutine with a temporary session cwd pin.

    The pin is active for the duration of the coroutine and automatically
    reset afterwards. Child tasks created by the coroutine inherit the pin:
    asyncio.create_task snapshots the current (pinned) context at creation.
    (The previous ctx.run() wrapping was wrong — Context.run drives a
    coroutine to completion synchronously and returns a plain value, so
    `await ctx.run(coro)` awaited a non-awaitable and never awaited the
    coroutine itself.)
    """
    token = pin_session_cwd(cwd)
    try:
        return await coro
    finally:
        reset_session_cwd(token)
