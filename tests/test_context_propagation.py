"""
Tests for context propagation utilities.

Run with: python -m pytest tests/test_context_propagation.py -v
"""

import asyncio
import contextvars

import pytest

from scripts.context_propagation import (
    ContextAwareLoop,
    bind_context,
    capture_context,
    run_with_context,
    schedule_with_context,
    spawn_context_thread,
    use_context,
)

# A contextvar to simulate session-scoped state (e.g., profile ID, auth token)
SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_id", default="launch-default"
)


def test_capture_and_run_restores_contextvar():
    """run_with_context restores the captured contextvar value."""
    SESSION_ID.set("session-42")
    ctx = capture_context()

    # Mutate in current context
    SESSION_ID.set("mutated")

    # Inside captured context, original value is visible
    def read() -> str:
        return SESSION_ID.get()

    assert run_with_context(ctx, read) == "session-42"
    # Current context unchanged
    assert SESSION_ID.get() == "mutated"


def test_bind_context_decorator():
    SESSION_ID.set("session-99")
    ctx = capture_context()

    @bind_context(ctx)
    def read() -> str:
        return SESSION_ID.get()

    SESSION_ID.set("other")
    assert read() == "session-99"


def test_spawn_context_thread_carries_context():
    """Background thread sees the contextvars from spawn time, not empty defaults."""
    SESSION_ID.set("session-thread-1")
    ctx = capture_context()
    result: dict[str, str] = {}

    def worker() -> None:
        result["value"] = SESSION_ID.get()

    t = spawn_context_thread(worker, name="test-worker", context=ctx)
    t.start()
    t.join(timeout=2)

    assert result["value"] == "session-thread-1", (
        "Thread should see captured session ID"
    )


def test_spawn_context_thread_without_explicit_context_uses_current():
    """If context not provided, current context is captured at spawn time."""
    SESSION_ID.set("session-implicit")
    result: dict[str, str] = {}

    def worker() -> None:
        result["value"] = SESSION_ID.get()

    t = spawn_context_thread(worker, name="test-implicit")
    t.start()
    t.join(timeout=2)

    assert result["value"] == "session-implicit"


@pytest.mark.asyncio
async def test_schedule_with_context_propagates_to_task():
    """Coroutine scheduled via schedule_with_context inherits contextvars."""
    SESSION_ID.set("session-async-1")
    ctx = capture_context()
    loop = asyncio.get_running_loop()

    async def reader() -> str:
        # Small yield to ensure task is fully scheduled
        await asyncio.sleep(0)
        return SESSION_ID.get()

    fut = schedule_with_context(reader(), loop, context=ctx)
    result = await fut
    assert result == "session-async-1"


@pytest.mark.asyncio
async def test_context_aware_loop_submit():
    SESSION_ID.set("session-loop-1")
    ctx = capture_context()
    loop = asyncio.get_running_loop()

    adapter_loop = ContextAwareLoop(loop, context=ctx)

    async def reader() -> str:
        await asyncio.sleep(0)
        return SESSION_ID.get()

    fut = adapter_loop.submit(reader())
    result = await fut
    assert result == "session-loop-1"


def test_use_context_manager():
    # use_context takes {ContextVar: value} bindings: a Context snapshot
    # has no __enter__ (that API does not exist), so block-scoped context
    # is expressed as set-values-inside/reset-on-exit.
    SESSION_ID.set("outside")
    with use_context({SESSION_ID: "session-cm-1"}):
        assert SESSION_ID.get() == "session-cm-1"
    assert SESSION_ID.get() == "outside"


def test_nested_contexts_isolation():
    """Inner context does not leak to outer."""
    SESSION_ID.set("outer")
    outer_ctx = capture_context()

    SESSION_ID.set("inner")
    inner_ctx = capture_context()

    def read_outer() -> str:
        return SESSION_ID.get()

    def read_inner() -> str:
        return SESSION_ID.get()

    assert run_with_context(outer_ctx, read_outer) == "outer"
    assert run_with_context(inner_ctx, read_inner) == "inner"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
