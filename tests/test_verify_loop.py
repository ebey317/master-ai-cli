"""Offline tests for verify_loop.py.

These tests never call OpenRouter, never run a real long pytest, and never
import master_ai or dispatch (avoiding circular imports).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

import verify_loop as vl


def test_enabled_reads_env():
    with mock.patch.dict(os.environ, {"MASTER_AI_VERIFY_LOOP": "1"}):
        assert vl.enabled() is True
    with mock.patch.dict(os.environ, {"MASTER_AI_VERIFY_LOOP": ""}, clear=False):
        assert vl.enabled() is False


def test_detect_test_command_with_tests_dir(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "foo.py").write_text("x = 1")
    cmd = vl.detect_test_command(tmp_path)
    assert cmd == [sys.executable, "-m", "pytest", "-q"]


def test_detect_test_command_with_test_file(tmp_path: Path):
    (tmp_path / "test_foo.py").write_text("def test_x(): pass")
    cmd = vl.detect_test_command(tmp_path)
    assert cmd == [sys.executable, "-m", "pytest", "-q"]


def test_detect_test_command_none(tmp_path: Path):
    (tmp_path / "foo.py").write_text("x = 1")
    assert vl.detect_test_command(tmp_path) is None


def test_run_tests_returns_tail(tmp_path: Path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_pass(): pass")
    exit_code, tail = vl.run_tests(tmp_path)
    assert exit_code == 0
    assert "1 passed" in tail


def test_run_tests_no_project(tmp_path: Path):
    exit_code, tail = vl.run_tests(tmp_path)
    assert exit_code == 0
    assert "no pytest project detected" in tail


def test_run_tests_timeout(tmp_path: Path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text(
        "import time\ndef test_slow(): time.sleep(10)\n"
    )
    monkeypatch.setattr(vl, "VERIFY_TIMEOUT", 0.1)
    exit_code, tail = vl.run_tests(tmp_path)
    assert exit_code == 124
    assert "timed out" in tail


def test_strip_edit_fences_removes_python_fence():
    find = "```python\nold\n```"
    replace = "```python\nnew\n```"
    assert vl.strip_edit_fences(find, replace) == ("old", "new")


def test_strip_edit_fences_no_fence():
    find = "old"
    replace = "new"
    assert vl.strip_edit_fences(find, replace) == ("old", "new")


def test_verify_feedback_pass():
    msg = vl.verify_feedback(0, "1 passed")
    assert msg.startswith("[AUTO-VERIFY RESULT] PASS")
    assert "1 passed" in msg


def test_verify_feedback_fail():
    msg = vl.verify_feedback(1, "1 failed")
    assert msg.startswith("[AUTO-VERIFY RESULT] FAIL")
    assert "1 failed" in msg


def test_post_edit_hook_appends_verify_result(tmp_path: Path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_y.py").write_text("def test_y(): pass")
    history: list[dict[str, str]] = []
    monkeypatch.setattr(vl, "MAX_VERIFY_ROUNDS", 5)
    with mock.patch.dict(os.environ, {"MASTER_AI_VERIFY_LOOP": "1"}):
        vl.post_edit_hook(history, cwd=tmp_path)
    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert "[AUTO-VERIFY RESULT] PASS" in history[0]["content"]


def test_post_edit_hook_disabled(tmp_path: Path):
    history: list[dict[str, str]] = []
    with mock.patch.dict(os.environ, {"MASTER_AI_VERIFY_LOOP": ""}, clear=False):
        vl.post_edit_hook(history, cwd=tmp_path)
    assert history == []


def test_post_edit_hook_caps_rounds(tmp_path: Path, monkeypatch):
    history = [
        {"role": "user", "content": "[AUTO-VERIFY RESULT] PASS\n1 passed"},
    ]
    monkeypatch.setattr(vl, "MAX_VERIFY_ROUNDS", 1)
    with mock.patch.dict(os.environ, {"MASTER_AI_VERIFY_LOOP": "1"}):
        vl.post_edit_hook(history, cwd=tmp_path)
    assert len(history) == 2
    assert "SKIPPED" in history[-1]["content"]


def test_explain_failures():
    assert "CREATE failed" in vl.explain_create_failure("denied", "/tmp/x.py")
    assert "EDIT failed" in vl.explain_edit_failure("text not found", "/tmp/x.py")


def test_api_summary():
    summary = vl.api_summary()
    assert "enabled" in summary
    assert "detected_command" in summary


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_flag_off_leaves_edit_text_untouched(monkeypatch):
    monkeypatch.delenv("MASTER_AI_VERIFY_LOOP", raising=False)
    find, rep = "    x = 1\n", "    x = 2\n"
    assert vl.normalize_edit_op("a.py", find, rep) == (find, rep)


def test_flag_on_keeps_indentation_of_unfenced_text(monkeypatch):
    monkeypatch.setenv("MASTER_AI_VERIFY_LOOP", "1")
    find, rep = "    x = 1\n", "    x = 2\n"
    assert vl.normalize_edit_op("a.py", find, rep) == (find, rep)
    fenced = "```python\n    x = 1\n```"
    assert vl.normalize_edit_op("a.py", fenced, "y")[0] == "    x = 1"
