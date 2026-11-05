"""2026-11-05 (Lijah): Sensei must NOT drop into plan/execute/critique 'agent mode'
on an ordinary message. The loop runs only on an explicit `agent: <task>`."""

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "master_ai.py").read_text(
    encoding="utf-8"
)


def test_the_multi_step_auto_router_is_gone():
    assert "_looks_multi_step" not in SRC


def test_agent_loop_is_only_started_by_the_explicit_prefix():
    calls = re.findall(r"^\s+handle_loop_task\(", SRC, re.M) + re.findall(
        r"^\s+reply = handle_loop_task\(", SRC, re.M
    )
    assert len(calls) == 1, f"handle_loop_task called from {len(calls)} places"
    i = SRC.index("handle_loop_task(task, history")
    assert '("agent:",)' in SRC[max(0, i - 900) : i]
