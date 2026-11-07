"""Unit tests for the frontier benchmark harness.

These tests run offline: they exercise task validity and the runner logic
with a fake model backend, never calling OpenRouter.
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


class _FakeRunner:
    """Inject into frontier.run to simulate a model that emits one action."""

    def __init__(self, reply: str):
        self.reply = reply


def test_runner_executes_actions_and_scores(monkeypatch, tmp_path: Path):
    """run_task scores a PASS when the model's actions make verify pass."""
    # Use task 10 because a single EDIT can fix it.
    task_dir = FRONTIER_DIR / "10_shared_state"
    fake_reply = """READ: registry.py
EDIT: registry.py
<<<FIND
class PluginRegistry:\n    _plugins = []  # BUG: mutable class-level state\n
    def __init__(self):\n        pass\n>>>FIND
<<<REPLACE
class PluginRegistry:\n    def __init__(self):\n        self._plugins = []\n>>>REPLACE
"""

    called: list[dict] = []

    def fake_chat(model, messages, api_key, timeout=120.0):
        called.append({"model": model, "message_count": len(messages)})
        return fake_reply

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
    assert result["passed"] is True
    assert result["model"] == "fake/model"
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["status"] == "pass"
    assert result["attempts"][0]["turns"] >= 1


def test_runner_scores_fail_when_model_does_nothing(monkeypatch, tmp_path: Path):
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
