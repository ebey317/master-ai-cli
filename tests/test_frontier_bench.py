"""Unit tests for the frontier benchmark harness.

These tests run offline: they exercise task validity, the runner logic,
fence-tolerant parsing, and the read-first guard with a fake model backend.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import eval.frontier.run as run_mod  # noqa: E402
import typed_actions  # noqa: E402

FRONTIER_DIR = REPO_ROOT / "eval" / "frontier"
RUNNER_PATH = FRONTIER_DIR / "run.py"


def _discover_tasks() -> list[Path]:
    return sorted(
        p for p in FRONTIER_DIR.iterdir() if p.is_dir() and (p / "repo").exists()
    )


@pytest.mark.parametrize("task_dir", _discover_tasks(), ids=lambda p: p.name)
def test_task_verify_fails_without_solution(task_dir: Path, tmp_path: Path):
    """The hidden verify suite must FAIL on the unmodified repo."""
    tmp = tmp_path / task_dir.name
    shutil.copytree(task_dir / "repo", tmp / "repo")
    shutil.copytree(task_dir / "verify", tmp / "verify")
    result = pytest_main(tmp / "verify")
    assert result.returncode != 0, f"verify should fail on unmodified {task_dir.name}"


@pytest.mark.parametrize("task_dir", _discover_tasks(), ids=lambda p: p.name)
def test_task_verify_passes_with_solution(task_dir: Path, tmp_path: Path):
    """Applying solution/patch.py must make verify PASS."""
    tmp = tmp_path / task_dir.name
    shutil.copytree(task_dir, tmp)
    _apply_patch(tmp)
    result = pytest_main(tmp / "verify")
    assert result.returncode == 0, (
        f"verify should pass after solution on {task_dir.name}"
    )


def _apply_patch(task_dir: Path) -> None:
    patch = task_dir / "solution" / "patch.py"
    spec = importlib.util.spec_from_file_location(f"patch_{task_dir.name}", patch)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.apply()


def pytest_main(verify_dir: Path):
    """Run pytest in-process and return its Session exit code proxy."""
    import subprocess

    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(verify_dir)],
        capture_output=True,
        text=True,
    )


def _fake_chat_factory(reply: str):
    """Return a fake _openrouter_chat that always returns `reply`."""

    def fake_chat(model, messages, api_key, timeout=120.0):
        return reply

    return fake_chat


def test_runner_executes_actions_and_scores(monkeypatch):
    """run_task scores a PASS when the model's actions make verify pass."""
    # Use task 10 because a single EDIT can fix it.
    fake_reply = (
        "READ: registry.py\n"
        "EDIT: registry.py\n"
        "<<<FIND\n"
        "class PluginRegistry:\n"
        "    _plugins = []  # BUG: mutable class-level state\n"
        "\n"
        "    def __init__(self):\n"
        "        pass\n"
        ">>>FIND\n"
        "<<<REPLACE\n"
        "class PluginRegistry:\n"
        "    def __init__(self):\n"
        "        self._plugins = []\n"
        ">>>REPLACE\n"
    )

    monkeypatch.setattr(run_mod, "_openrouter_chat", _fake_chat_factory(fake_reply))
    monkeypatch.setattr(run_mod, "_load_keychain_openrouter_key", lambda: "fake-key")

    result = run_mod.run_task(
        "10_shared_state",
        "fake/model",
        "fake-key",
        max_turns=3,
        timeout=60.0,
        k=1,
    )
    assert result["passed"] is True
    assert result["model"] == "fake/model"
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["status"] == "pass"
    assert result["attempts"][0]["turns"] >= 1
    assert result["attempts"][0]["landed_edits"] >= 1


def test_runner_scores_fail_when_model_does_nothing(monkeypatch):
    """run_task scores FAIL when the model emits no actions."""

    def fake_chat(model, messages, api_key, timeout=120.0):
        return "I think this is beyond me."

    monkeypatch.setattr(run_mod, "_openrouter_chat", fake_chat)
    monkeypatch.setattr(run_mod, "_load_keychain_openrouter_key", lambda: "fake-key")

    result = run_mod.run_task(
        "10_shared_state",
        "fake/model",
        "fake-key",
        max_turns=3,
        timeout=60.0,
        k=1,
    )
    assert result["passed"] is False
    assert result["attempts"][0]["status"] == "fail"


def test_read_guard_rejects_edit_before_read(monkeypatch):
    """EDIT on a file that was never READ is rejected and counted as failed."""
    fake_reply = (
        "EDIT: registry.py\n"
        "<<<FIND\n"
        "    def __init__(self):\n"
        "        pass\n"
        ">>>FIND\n"
        "<<<REPLACE\n"
        "    def __init__(self):\n"
        "        self._plugins = []\n"
        ">>>REPLACE\n"
    )

    monkeypatch.setattr(run_mod, "_openrouter_chat", _fake_chat_factory(fake_reply))
    monkeypatch.setattr(run_mod, "_load_keychain_openrouter_key", lambda: "fake-key")

    result = run_mod.run_task(
        "10_shared_state",
        "fake/model",
        "fake-key",
        max_turns=2,
        timeout=60.0,
        k=1,
    )
    assert result["passed"] is False
    attempt = result["attempts"][0]
    assert attempt["failed_edits"] >= 1
    assert (
        "must READ" in attempt["verifier_tail"].lower() or attempt["action_errors"] >= 1
    )


def test_legacy_flag_bypasses_read_guard(monkeypatch):
    """With --legacy, an EDIT without a prior READ is allowed to land."""
    fake_reply = (
        "EDIT: registry.py\n"
        "<<<FIND\n"
        "class PluginRegistry:\n"
        "    _plugins = []  # BUG: mutable class-level state\n"
        "\n"
        "    def __init__(self):\n"
        "        pass\n"
        ">>>FIND\n"
        "<<<REPLACE\n"
        "class PluginRegistry:\n"
        "    def __init__(self):\n"
        "        self._plugins = []\n"
        ">>>REPLACE\n"
    )

    monkeypatch.setattr(run_mod, "_openrouter_chat", _fake_chat_factory(fake_reply))
    monkeypatch.setattr(run_mod, "_load_keychain_openrouter_key", lambda: "fake-key")

    result = run_mod.run_task(
        "10_shared_state",
        "fake/model",
        "fake-key",
        max_turns=3,
        timeout=60.0,
        k=1,
        legacy=True,
    )
    assert result["passed"] is True
    assert result["attempts"][0]["landed_edits"] >= 1


def test_strip_fences_whole_block():
    """A directive block wrapped in ```python ... ``` is parsed correctly."""
    fenced = """```python
CREATE: expr.py
<<<CONTENT
a = 1
>>>CONTENT
```"""
    stripped = run_mod._strip_fences(fenced)
    actions = typed_actions.parse_reply_with_bodies(stripped)
    kinds = [a.kind for a in actions]
    assert typed_actions.Kind.CREATE in kinds
    create = [a for a in actions if a.kind == typed_actions.Kind.CREATE][0]
    assert create.create_content.strip() == "a = 1"


def test_strip_fences_body_fence():
    """A CREATE body wrapped in ```python is stripped."""
    fenced = """CREATE: expr.py
<<<CONTENT
```python
a = 1
```
>>>CONTENT"""
    stripped = run_mod._strip_fences(fenced)
    actions = typed_actions.parse_reply_with_bodies(stripped)
    create = [a for a in actions if a.kind == typed_actions.Kind.CREATE][0]
    assert create.create_content.strip() == "a = 1"


def test_strip_fences_edit_body_fence():
    """An EDIT find/replace block wrapped in ``` is stripped."""
    fenced = """READ: registry.py
EDIT: registry.py
<<<FIND
```
    pass
```
>>>FIND
<<<REPLACE
```
    self._plugins = []
```
>>>REPLACE"""
    stripped = run_mod._strip_fences(fenced)
    actions = typed_actions.parse_reply_with_bodies(stripped)
    edits = [a for a in actions if a.kind == typed_actions.Kind.EDIT]
    assert len(edits) == 1
    assert edits[0].edit_old.strip() == "pass"
    assert edits[0].edit_new.strip() == "self._plugins = []"


def test_auto_verify_stops_early(monkeypatch):
    """After a successful EDIT the harness auto-verifies and stops on PASS."""
    fake_reply = (
        "READ: registry.py\n"
        "EDIT: registry.py\n"
        "<<<FIND\n"
        "class PluginRegistry:\n"
        "    _plugins = []  # BUG: mutable class-level state\n"
        "\n"
        "    def __init__(self):\n"
        "        pass\n"
        ">>>FIND\n"
        "<<<REPLACE\n"
        "class PluginRegistry:\n"
        "    def __init__(self):\n"
        "        self._plugins = []\n"
        ">>>REPLACE\n"
    )

    monkeypatch.setattr(run_mod, "_openrouter_chat", _fake_chat_factory(fake_reply))
    monkeypatch.setattr(run_mod, "_load_keychain_openrouter_key", lambda: "fake-key")

    result = run_mod.run_task(
        "10_shared_state",
        "fake/model",
        "fake-key",
        max_turns=5,
        timeout=60.0,
        k=1,
    )
    assert result["passed"] is True
    assert result["attempts"][0]["turns"] <= 2


def test_discover_tasks_all():
    assert "01_off_by_one" in run_mod._discover_tasks("all")
    assert "10_shared_state" in run_mod._discover_tasks("all")


def test_discover_tasks_subset():
    assert run_mod._discover_tasks("01_off_by_one,02_spec_feature") == [
        "01_off_by_one",
        "02_spec_feature",
    ]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
