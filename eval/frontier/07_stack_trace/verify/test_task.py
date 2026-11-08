import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from handlers import handle_greet
from runner import run_task


def test_run_task_strips_prefix():
    assert run_task("t-42", handle_greet) == "done 42"


def test_run_task_no_prefix():
    assert run_task("42", handle_greet) == "done 42"
