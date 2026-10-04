"""
Context propagation utilities for Sensei.

Ensures that contextvars (session ID, profile config, auth tokens, feature flags)
carry into background threads and cross-thread async scheduling, preventing
"empty context" fallback to launch-time defaults.
"""
from __future__ import annotations

import contextvars
import threading
import functools
from typing import Any, Callable, Optional, TypeVar, ParamSpec, Awaitable
import asyncio

P = ParamSpec("P")
R = TypeVar("R")

# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def capture_context() -> contextvars.Context:
    """Snapshot the current contextvars. Call at a well-defined entry point (e.g. session start, RPC connect)."""
    return contextvars.copy_context()


def run_with_context(ctx: contextvars.Context, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Execute *func* inside *ctx* (blocking)."""
    return ctx.run(func, *args, **kwargs)


def bind_context(ctx: contextvars.Context) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator: run the wrapped function under *ctx*."""
    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return ctx.run(fn, *args, **kwargs)
        return wrapper
    return decorator


def spawn_context_thread(
    target: Callable[P, Any],
    *,
    name: str,
    daemon: bool = True,
    args: tuple = (),
    kwargs: Optional[dict] = None,
    context: Optional[contextvars.Context] = None,
) -> threading.Thread:
    """
    Return an unstarted ``threading.Thread`` that runs *target* under the captured context.

    Usage:
        ctx = capture_context()          # at request/session entry
        t = spawn_context_thread(worker, name="bg-worker", context=ctx)
        t.start()
    """
    ctx = context or contextvars.copy_context()
    bound_target = bind_context(ctx)(target)
    return threading.Thread(
        target=bound_target,
        name=name,
        daemon=daemon,
        args=args,
        kwargs=kwargs or {},
    )


# ----------------------------------------------------------------------
# Async helpers (for run_coroutine_threadsafe / loop.call_soon_threadsafe)
# ----------------------------------------------------------------------


def schedule_with_context(
    coro: Awaitable[Any],
    loop: asyncio.AbstractEventLoop,
    *,
    context: Optional[contextvars.Context] = None,
) -> asyncio.Future:
    """
    Schedule *coro* on *loop* with *context* (or current context) applied to the scheduled task.

    This wraps ``asyncio.run_coroutine_threadsafe`` so the *target task* (and any
    ``create_task``/``to_thread`` it spawns) inherits the intended contextvars.
    """
    ctx = context or contextvars.copy_context()

    def _submit() -> asyncio.Future:
        return asyncio.run_coroutine_threadsafe(coro, loop)

    # Run the submission itself inside the context so the returned Future's task
    # carries the contextvars (Python 3.11+ propagates context to the created task).
    return ctx.run(_submit)


class ContextAwareLoop:
    """
    Thin wrapper around an event loop that captures a context at construction
    and applies it to every cross-thread scheduling call.

    Intended for adapters / gateways that receive callbacks on foreign threads
    (gRPC, Pub/Sub, WebSocket, signal handlers).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, context: Optional[contextvars.Context] = None):
        self._loop = loop
        self._ctx = context or contextvars.copy_context()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def submit(self, coro: Awaitable[Any]) -> asyncio.Future:
        return schedule_with_context(coro, self._loop, context=self._ctx)

    def call_soon(self, callback: Callable[..., Any], *args: Any) -> asyncio.Handle:
        """Schedule a plain callback with context applied."""
        return self._ctx.run(self._loop.call_soon_threadsafe, callback, *args)

    def call_later(self, delay: float, callback: Callable[..., Any], *args: Any) -> asyncio.TimerHandle:
        return self._ctx.run(self._loop.call_later, delay, callback, *args)


# ----------------------------------------------------------------------
# Convenience: context manager for ad-hoc binding
# ----------------------------------------------------------------------


class use_context:
    """``with use_context(ctx): ...`` — run a block under *ctx*."""

    def __init__(self, ctx: contextvars.Context):
        self._ctx = ctx
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> None:
        self._token = self._ctx.__enter__()

    def __exit__(self, *exc: Any) -> None:
        self._ctx.__exit__(None, None, None)
