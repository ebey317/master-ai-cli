"""
Async utilities for running coroutines from sync contexts while preserving contextvars.

When already inside a running event loop, `asyncio.run()` must execute on a separate thread.
A bare `ThreadPoolExecutor` starts workers with an empty contextvars.Context, losing the
caller's profile scope, secret scope, and other context variables. This module provides
a helper that copies the current context into the worker thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")


def run_async_preserving_context(
    coro: Awaitable[_T], *, timeout: float | None = None
) -> _T:
    """
    Run an async coroutine from a synchronous context, preserving contextvars.

    If there is no running event loop, runs directly via asyncio.run().
    If there is a running loop, offloads to a single-thread ThreadPoolExecutor
    with the caller's contextvars copied into the worker thread.

    Args:
        coroutine: The awaitable to execute.
        timeout: Optional timeout in seconds for the thread-pool future.

    Returns:
        The coroutine's result.

    Raises:
        Any exception raised by the coroutine, or concurrent.futures.TimeoutError.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to use asyncio.run directly in this thread.
        # A timeout here still must be honored: wrap in wait_for and normalize
        # asyncio.TimeoutError to the builtin (they are distinct classes on
        # Python <3.11, and callers catch the builtin).
        if timeout is None:
            return asyncio.run(coro)  # type: ignore[arg-type]
        try:
            return asyncio.run(asyncio.wait_for(coro, timeout))  # type: ignore[arg-type]
        except asyncio.TimeoutError:
            raise TimeoutError(f"coroutine did not finish within {timeout}s") from None

    # Running loop detected: we must hop to a side thread for asyncio.run().
    # Copy the current contextvars.Context so profile/secrets scoping survives the hop.
    ctx = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future: concurrent.futures.Future[_T] = pool.submit(ctx.run, asyncio.run, coro)  # type: ignore[arg-type]
        try:
            return future.result(timeout=timeout)  # type: ignore[no-any-return]
        except concurrent.futures.TimeoutError:
            # Normalize: on Python <3.11 concurrent.futures.TimeoutError is
            # NOT the builtin TimeoutError, so callers doing
            # `pytest.raises(TimeoutError)` (or `except TimeoutError`) would
            # miss it. Re-raise the builtin unconditionally.
            raise TimeoutError(f"coroutine did not finish within {timeout}s") from None


def run_async_preserving_context_no_timeout(coro: Awaitable[_T]) -> _T:
    """Convenience wrapper without timeout (matches upstream pattern)."""
    return run_async_preserving_context(coro, timeout=None)
