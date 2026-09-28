"""Tests for the reload-carry note restored on startup.

When master_ai.py live-reloads, it writes the operator's in-flight
instruction to ~/.master_ai_reload_carry and restarts. The new process
consumes that file on startup via _restore_reload_carry().

Two defects motivated these tests:

- A stale carry from an old session would revive it. Anything older than
  RESUME_FLAG_MAX_AGE must be discarded.
- A carry file whose read fails was never unlinked, because unlink ran
  after read_text(). The file survived and re-logged
  AUTO_RELOAD_CARRY_RESTORE_ERROR on every startup until someone deleted
  it by hand. unlink() must happen even when the read raises.

Extracted 2026-09-28 out of main()'s startup block; as an inline block it
had no seam and neither behaviour was testable.

These drive the real function against real files and assert the return
value and the resulting on-disk state.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def ma(monkeypatch):
    import master_ai

    logged = []
    monkeypatch.setattr(master_ai, "log", lambda m: logged.append(m))
    master_ai._test_log = logged
    return master_ai


def _write(path: Path, text: str, age_s: float = 0.0):
    path.write_text(text)
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))


# ── the happy path ──


def test_fresh_carry_is_returned(ma, tmp_path):
    carry = tmp_path / "carry"
    _write(carry, "finish the task list")
    assert ma._restore_reload_carry(carry) == "finish the task list"


def test_carry_is_consumed(ma, tmp_path):
    """The file is removed after a successful read, so it cannot replay."""
    carry = tmp_path / "carry"
    _write(carry, "resume me")
    ma._restore_reload_carry(carry)
    assert not carry.exists()


def test_second_restore_of_the_same_file_returns_none(ma, tmp_path):
    """A carry note fires once, not on every subsequent startup."""
    carry = tmp_path / "carry"
    _write(carry, "once")
    assert ma._restore_reload_carry(carry) == "once"
    assert ma._restore_reload_carry(carry) is None


def test_missing_file_is_a_noop(ma, tmp_path):
    assert ma._restore_reload_carry(tmp_path / "absent") is None


def test_empty_file_yields_none_but_is_still_consumed(ma, tmp_path):
    carry = tmp_path / "carry"
    _write(carry, "")
    assert ma._restore_reload_carry(carry) is None
    assert not carry.exists()


# ── staleness ──


def test_stale_carry_is_discarded(ma, tmp_path):
    """A flag from hours ago must not revive an old session."""
    carry = tmp_path / "carry"
    _write(carry, "ancient", age_s=ma.RESUME_FLAG_MAX_AGE + 60)
    assert ma._restore_reload_carry(carry) is None
    assert not carry.exists()
    assert any("CARRY_EXPIRED" in m for m in ma._test_log), ma._test_log


def test_carry_just_inside_the_window_is_kept(ma, tmp_path):
    """The boundary must not discard a note that is still fresh enough."""
    carry = tmp_path / "carry"
    _write(carry, "still good", age_s=ma.RESUME_FLAG_MAX_AGE - 30)
    assert ma._restore_reload_carry(carry) == "still good"


# ── unreadable carry file ──


def test_unreadable_carry_is_removed_and_does_not_raise(ma, tmp_path, monkeypatch):
    """The regression: a failed read must still unlink, and never raise.

    Before the fix, unlink ran after read_text(), so a read failure left
    the file in place and re-logged on every startup.
    """
    carry = tmp_path / "carry"
    _write(carry, "unreadable")

    def _boom(self, *a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)

    assert ma._restore_reload_carry(carry) is None
    assert not carry.exists(), "carry survived a failed read"
    assert any("AUTO_RELOAD_CARRY_RESTORE_ERROR" in m for m in ma._test_log)


def test_unreadable_carry_does_not_re_log_on_the_next_startup(
    ma, tmp_path, monkeypatch
):
    """After one bad read, startup must be quiet -- the file is gone."""
    carry = tmp_path / "carry"
    _write(carry, "unreadable")
    monkeypatch.setattr(
        Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    ma._restore_reload_carry(carry)

    ma._test_log.clear()
    monkeypatch.undo()
    assert ma._restore_reload_carry(carry) is None
    assert not any("AUTO_RELOAD_CARRY_RESTORE_ERROR" in m for m in ma._test_log)


def test_stat_failure_is_swallowed(ma, tmp_path, monkeypatch):
    """A carry path we cannot even stat must not stop startup."""
    carry = tmp_path / "carry"
    _write(carry, "x")

    def _boom(self, *a, **k):
        raise OSError("cannot stat")

    monkeypatch.setattr(Path, "stat", _boom)
    assert ma._restore_reload_carry(carry) is None


def test_default_path_is_the_real_carry_file(ma, monkeypatch, tmp_path):
    """With no argument the real ~/.master_ai_reload_carry is used."""
    carry = tmp_path / "carry"
    _write(carry, "default path")
    monkeypatch.setattr(ma, "_RELOAD_CARRY_FILE", carry)
    assert ma._restore_reload_carry() == "default path"
    assert not carry.exists()
