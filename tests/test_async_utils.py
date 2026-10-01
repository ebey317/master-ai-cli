"""
Tests for contextvars preservation across thread-pool asyncio.run hops.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import sys
from pathlib import Path

import pytest

# Ensure repo root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.async_utils import run_async_preserving_context, run_async_preserving_context_no_timeout


# A contextvar to simulate profile-scoped state (e.g., HERMES_HOME override, secret scope)
_current_profile = contextvars.ContextVar("current_profile", default="launch")


def test_direct_asyncio_run_preserves_context():
    """Baseline: asyncio.run in a fresh thread inherits the caller's context when using our helper."""
    token = _current_profile.set("served-profile")
    try:
        async def read_profile() -> str:
            return _current_profile.get()

        result = run_async_preserving_context_no_timeout(read_profile())
        assert result == "served-profile"
    finally:
        _current_profile.reset(token)


def test_side_thread_from_running_loop_preserves_context():
    """Main test: inside a running loop, the side-thread hop must keep contextvars."""
    async def outer():
        # Simulate being inside the agent's running event loop (gateway/TUI turn)
        token = _current_profile.set("served-profile")
        try:
            # This calls our helper, which detects the running loop and uses a thread pool
            async def inner() -> str:
                return _current_profile.get()

            result = run_async_preserving_context_no_timeout(inner())
            assert result == "served-profile", f"expected served-profile, got {result}"
        finally:
            _current_profile.reset(token)

    asyncio.run(outer())


def test_nested_contextvars_survive_multiple_hops():
    """Multiple contextvars (profile + secrets scope) all survive the hop."""
    profile_var = contextvars.ContextVar("profile", default="launch")
    secrets_scope_var = contextvars.ContextVar("secrets_scope", default="global")

    async def outer():
        token_p = profile_var.set("profile-b")
        token_s = secrets_scope_var.set("profile-b-secrets")
        try:
            async def inner() -> tuple[str, str]:
                return profile_var.get(), secrets_scope_var.get()

            prof, scope = run_async_preserving_context_no_timeout(inner())
            assert prof == "profile-b"
            assert scope == "profile-b-secrets"
        finally:
            profile_var.reset(token_p)
            secrets_scope_var.reset(token_s)

    asyncio.run(outer())


def test_timeout_propagates():
    """Timeout parameter is respected."""
    async def slow():
        await asyncio.sleep(10)
        return "done"

    with pytest.raises(TimeoutError):
        run_async_preserving_context(slow(), timeout=0.01)


def test_exception_propagates():
    """Exceptions from the coroutine propagate through the helper."""
    async def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_async_preserving_context_no_timeout(fail())


def test_no_running_loop_uses_asyncio_run_directly():
    """When no loop is running, asyncio.run is used directly (no thread pool)."""
    # This test runs in a fresh process context via pytest-asyncio, so no loop is running
    token = _current_profile.set("direct-profile")
    try:
        async def read_profile() -> str:
            return _current_profile.get()

        result = run_async_preserving_context_no_timeout(read_profile())
        assert result == "direct-profile"
    finally:
        _current_profile.reset(token)
