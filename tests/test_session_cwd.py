"""Tests for session-scoped CWD context management."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from scripts.session_cwd import (
    get_session_cwd,
    reset_session_cwd,
    resolve_effective_cwd,
    session_cwd_scope,
    set_session_cwd,
)


def test_session_cwd_basic_set_get():
    token = set_session_cwd("/tmp/test")
    try:
        assert get_session_cwd() == "/tmp/test"
    finally:
        reset_session_cwd(token)
    assert get_session_cwd() == ""


def test_session_cwd_context_manager(tmp_path: Path):
    test_dir = tmp_path / "repo"
    test_dir.mkdir()

    with session_cwd_scope(test_dir):
        assert get_session_cwd() == str(test_dir)
        assert resolve_effective_cwd() == test_dir.resolve()

    assert get_session_cwd() == ""


def test_session_cwd_nested_scopes(tmp_path: Path):
    dir1 = tmp_path / "repo1"
    dir2 = tmp_path / "repo2"
    dir1.mkdir()
    dir2.mkdir()

    with session_cwd_scope(dir1):
        assert resolve_effective_cwd() == dir1.resolve()
        with session_cwd_scope(dir2):
            assert resolve_effective_cwd() == dir2.resolve()
        assert resolve_effective_cwd() == dir1.resolve()
    assert get_session_cwd() == ""


def test_resolve_effective_cwd_fallback_chain(tmp_path: Path):
    # No session CWD -> process CWD
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        assert resolve_effective_cwd().resolve() == Path(td).resolve()

    # Session CWD wins over process CWD
    with session_cwd_scope(tmp_path):
        assert resolve_effective_cwd() == tmp_path.resolve()

    # Explicit fallback used when nothing else
    assert resolve_effective_cwd(fallback="/explicit/fallback") == Path("/explicit/fallback")
