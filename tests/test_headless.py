"""Tests for the headless task/delegation mode."""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import headless_runner


def test_headless_runner_loads_task_from_string():
    runner = headless_runner.HeadlessRunner(task="Say hello")
    assert runner._load_task() == "Say hello"


def test_headless_runner_loads_task_from_file(tmp_path):
    task_file = tmp_path / "task.md"
    task_file.write_text("List files")
    runner = headless_runner.HeadlessRunner(task_file=str(task_file))
    assert runner._load_task() == "List files"


def test_headless_runner_main_rejects_missing_task(capsys):
    assert headless_runner.main(["--headless"]) == 1
    captured = capsys.readouterr()
    assert "--task or --task-file is required" in captured.out


def test_fallback_parse_extracts_prefixes():
    reply = (
        "READ: README.md\n"
        "CREATE: foo.txt\n"
        "EDIT: bar.py\n"
        "RUN: ls -la\n"
        "RUNTERM: echo hi\n"
        "SUBAGENT: file_finder: find config\n"
    )
    actions = headless_runner.HeadlessRunner._fallback_parse(reply)
    types = [a["type"] for a in actions]
    assert types == ["read", "create", "edit", "run", "runterm", "subagent"]
    assert actions[0]["target"] == "README.md"
    assert actions[5]["name"] == "file_finder: find config"


def test_execute_read_action_reads_existing_file(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text("hello")
    result = headless_runner._read_file(str(sample))
    assert "hello" in result


def test_execute_create_action_writes_file(tmp_path):
    target = tmp_path / "created.txt"
    result = headless_runner._create_file(str(target), "data")
    assert target.read_text() == "data"
    assert "Created" in result


def test_run_shell_blocks_dangerous_command():
    result = headless_runner._run_shell("rm -rf /")
    assert "Blocked" in result


def test_run_shell_runs_safe_command():
    result = headless_runner._run_shell("echo hi")
    assert "hi" in result
