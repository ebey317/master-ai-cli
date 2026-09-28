"""Tests for session-scoped CWD context management."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

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
    assert resolve_effective_cwd(fallback="/explicit/fallback") == Path(
        "/explicit/fallback"
    )


def test_resolve_effective_cwd_survives_a_deleted_process_cwd(tmp_path: Path):
    """A deleted process CWD must fall through, not raise.

    Path.cwd() raises FileNotFoundError once the directory the process is
    sitting in has been removed, which happens with any TemporaryDirectory
    that has exited or a scratch dir that got cleaned. The fallback chain
    exists for exactly that, so it has to be reachable.
    """
    token = set_session_cwd("")
    try:
        victim = tmp_path / "doomed"
        victim.mkdir()
        os.chdir(victim)
        victim.rmdir()  # cwd is now a deleted inode

        assert resolve_effective_cwd(fallback="/explicit/fallback") == Path(
            "/explicit/fallback"
        )
        # And with no fallback at all it still returns something usable.
        assert resolve_effective_cwd() == Path.home()
    finally:
        reset_session_cwd(token)
        os.chdir(Path.home())
