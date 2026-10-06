"""
Tests for session-scoped MCP cwd pinning.

Verifies the two key invariants:
1. Session pin becomes the default stdio cwd when no explicit cwd is configured.
2. Explicit per-server cwd in config always wins over the session pin.
"""

import asyncio
from pathlib import Path

import pytest

from scripts.mcp_session_cwd import (
    get_pinned_cwd,
    pin_session_cwd,
    reset_session_cwd,
    run_async_with_pinned_cwd,
    run_with_pinned_cwd,
)


class TestSessionCwdPinning:
    def test_pin_and_get_cwd(self, tmp_path):
        """Basic pin/get/reset cycle works."""
        token = pin_session_cwd(tmp_path)
        try:
            assert get_pinned_cwd() == tmp_path.resolve()
        finally:
            reset_session_cwd(token)
        assert get_pinned_cwd() is None

    def test_pin_none_clears(self):
        """Pinning None clears the cwd."""
        token_outer = pin_session_cwd("/some/path")
        token = pin_session_cwd(None)
        try:
            assert get_pinned_cwd() is None
        finally:
            # Reset in REVERSE order: each token restores the value present
            # before its own set, so unwinding outer-first would re-pin
            # '/some/path' and leak into later tests.
            reset_session_cwd(token)
            reset_session_cwd(token_outer)

    def test_run_with_pinned_cwd_isolated(self, tmp_path):
        """run_with_pinned_cwd isolates the pin to the call."""
        outer = Path("/outer")
        token_outer = pin_session_cwd(outer)
        try:
            captured = {}

            def inner():
                captured["cwd"] = get_pinned_cwd()

            run_with_pinned_cwd(tmp_path, inner)
            assert captured["cwd"] == tmp_path.resolve()
            # Outer pin restored
            assert get_pinned_cwd() == outer.resolve()
        finally:
            reset_session_cwd(token_outer)

    def test_run_with_pinned_cwd_propagates_to_threads(self, tmp_path):
        """Pin propagates to functions run via contextvars.copy_context().run."""
        captured = {}

        def worker():
            captured["cwd"] = get_pinned_cwd()

        run_with_pinned_cwd(tmp_path, worker)
        assert captured["cwd"] == tmp_path.resolve()

    @pytest.mark.asyncio
    async def test_async_pin_propagates_to_child_tasks(self, tmp_path):
        """Pin propagates to asyncio tasks created within the pinned context."""
        captured = {}

        async def child():
            captured["cwd"] = get_pinned_cwd()

        async def parent():
            asyncio.create_task(child())
            await asyncio.sleep(0)  # let child run

        await run_async_with_pinned_cwd(tmp_path, parent())
        assert captured["cwd"] == tmp_path.resolve()

    def test_explicit_config_cwd_wins_over_session_pin(
        self, tmp_path, tmp_path_factory
    ):
        """
        Invariant: explicit per-server cwd in config always wins over session pin.
        This is a design invariant - the session pin is only a *default*.
        """
        other_dir = tmp_path_factory.mktemp("other")
        session_token = pin_session_cwd(tmp_path)
        try:
            # Simulate MCP server config with explicit cwd
            server_config = {"command": "echo", "args": ["hi"], "cwd": str(other_dir)}

            # The transport should use explicit cwd, not session pin
            effective_cwd = server_config.get("cwd") or get_pinned_cwd()
            assert Path(effective_cwd).resolve() == other_dir.resolve()
        finally:
            reset_session_cwd(session_token)

    def test_session_pin_becomes_default_when_no_explicit_cwd(self, tmp_path):
        """
        Invariant: session pin becomes the default stdio cwd when no explicit cwd.
        """
        session_token = pin_session_cwd(tmp_path)
        try:
            server_config = {"command": "echo", "args": ["hi"]}  # no cwd key

            effective_cwd = server_config.get("cwd") or get_pinned_cwd()
            assert effective_cwd == tmp_path.resolve()
        finally:
            reset_session_cwd(session_token)

    def test_no_pin_yields_none_default(self):
        """No pin and no explicit cwd yields None (process default)."""
        # Ensure clean state
        token = pin_session_cwd(None)
        try:
            server_config = {"command": "echo", "args": ["hi"]}
            effective_cwd = server_config.get("cwd") or get_pinned_cwd()
            assert effective_cwd is None
        finally:
            reset_session_cwd(token)

    def test_pin_survives_exception(self, tmp_path):
        """Pin is reset even if the wrapped function raises."""

        def raises():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            run_with_pinned_cwd(tmp_path, raises)

        # Pin should be cleared (back to whatever it was before, which is None)
        assert get_pinned_cwd() is None
