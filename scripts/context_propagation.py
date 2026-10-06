"""
Context propagation utilities for Sensei.

Ensures that contextvars (session ID, profile config, auth tokens, feature flags)
carry into background threads and cross-thread async scheduling, preventing
"empty context" fallback to launch-time defaults.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def capture_context() -> contextvars.Context:
    """Snapshot the current contextvars. Call at a well-defined entry point (e.g. session start, RPC connect)."""
    return contextvars.copy_context()


def run_with_context(
    ctx: contextvars.Context, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> R:
    """Execute *func* inside *ctx* (blocking)."""
    return ctx.run(func, *args, **kwargs)


def bind_context(
    ctx: contextvars.Context,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
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
    kwargs: dict | None = None,
    context: contextvars.Context | None = None,
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
    context: contextvars.Context | None = None,
) -> asyncio.Future:
    """
    Schedule *coro* on *loop* with *context* (or current context) applied to the scheduled task.

    This wraps ``asyncio.run_coroutine_threadsafe`` so the *target task* (and any
    ``create_task``/``to_thread`` it spawns) inherits the intended contextvars.
    """
    ctx = context or contextvars.copy_context()

    def _submit() -> asyncio.Future:
        # run_coroutine_threadsafe returns a concurrent.futures.Future —
        # awaitable only after bridging. wrap_future converts it to a real
        # asyncio.Future on the caller's running loop, so `await fut` works.
        # (the coro param is an Awaitable by contract; run_coroutine_threadsafe
        # narrows to Coroutine — mypy can't see that every caller passes one)
        return asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]
        )

    # Run the submission inside the captured context. NOTE: this propagates
    # the context to the *submission*; the created task's own context behavior
    # is interpreter-version-dependent, which the propagation test verifies.
    return ctx.run(_submit)


class ContextAwareLoop:
    """
    Thin wrapper around an event loop that captures a context at construction
    and applies it to every cross-thread scheduling call.

    Intended for adapters / gateways that receive callbacks on foreign threads
    (gRPC, Pub/Sub, WebSocket, signal handlers).
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        context: contextvars.Context | None = None,
    ):
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

    def call_later(
        self, delay: float, callback: Callable[..., Any], *args: Any
    ) -> asyncio.TimerHandle:
        return self._ctx.run(self._loop.call_later, delay, callback, *args)


# ----------------------------------------------------------------------
# Convenience: context manager for ad-hoc binding
# ----------------------------------------------------------------------


class use_context:
    """``with use_context(bindings, ...): ...`` — apply contextvar values for a block.

    A captured contextvars.Context is a *frozen snapshot* with no
    ``__enter__``/``__exit__`` (that API does not exist in any Python
    version), so a Context object cannot wrap a ``with`` block directly.
    The block-scoped equivalent is binding values: pass a mapping of
    ContextVar -> value; ``__enter__`` sets each and records tokens,
    ``__exit__`` resets them in reverse order, restoring whatever the
    caller had before (values or unset).
    """

    def __init__(self, bindings: dict[contextvars.ContextVar, Any]):
        self._bindings = dict(bindings)
        self._tokens: list[tuple[contextvars.ContextVar, contextvars.Token]] = []

    def __enter__(self) -> dict[contextvars.ContextVar, contextvars.Token]:
        tokens: dict[contextvars.ContextVar, contextvars.Token] = {}
        for var, value in self._bindings.items():
            tokens[var] = var.set(value)
            self._tokens.append((var, tokens[var]))
        return tokens

    def __exit__(self, *exc: Any) -> None:
        # Reverse order keeps the restore stack consistent if one var was
        # rebound inside another's scope.
        for var, token in reversed(self._tokens):
            var.reset(token)
        self._tokens.clear()
