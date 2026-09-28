"""Wiring tests for the Tier-1 pre-dispatch validation gate in master_ai.

The validator itself is covered by test_typed_dispatch_e2e.py and
action_validation.py. What matters here is that the gate is actually
*reachable* and *effective* in process_reply — the failure mode being guarded
against is a gate that exists but never fires, or fires after dispatch.

These drive the real `_validation_gate` on the real list shapes that
process_reply's extraction produces, and assert on what survives to dispatch.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ma():
    import master_ai

    return master_ai


def test_valid_actions_all_survive(ma):
    kept, blocked = ma._validation_gate(
        {
            "run_cmds": ["ls -la /tmp", "python3 main.py", "cd /tmp && ls"],
            "read_paths": ["/tmp/notes.md"],
            "create_files": [("/tmp/ok.py", "def f():\n    return 1")],
        }
    )
    assert blocked == []
    assert kept["run_cmds"] == ["ls -la /tmp", "python3 main.py", "cd /tmp && ls"]
    assert kept["read_paths"] == ["/tmp/notes.md"]


def test_narrated_run_never_reaches_dispatch(ma):
    """The headline Tier-1 case: prose must not become a command."""
    kept, blocked = ma._validation_gate(
        {"run_cmds": ["ls -la", "let me run the test suite", "cd /tmp"]}
    )
    assert kept["run_cmds"] == ["ls -la", "cd /tmp"], kept["run_cmds"]
    assert len(blocked) == 1
    assert "sentence" in blocked[0][2]


def test_unparseable_file_never_reaches_dispatch(ma):
    kept, blocked = ma._validation_gate(
        {
            "create_files": [
                ("/tmp/bad.py", "def broken("),
                ("/tmp/good.py", "x = 1"),
            ]
        }
    )
    assert [p[0] for p in kept["create_files"]] == ["/tmp/good.py"]
    assert blocked[0][0] == "create_files"
    assert "syntax error" in blocked[0][2]


def test_bad_shell_is_blocked(ma):
    kept, blocked = ma._validation_gate({"runterm_cmds": ["if [ -f x ; then echo hi"]})
    assert kept["runterm_cmds"] == []
    assert "shell syntax error" in blocked[0][2]


def test_order_is_preserved(ma):
    kept, _b = ma._validation_gate(
        {"run_cmds": ["ls -1", "echo two", "pwd", "echo four"]}
    )
    assert kept["run_cmds"] == ["ls -1", "echo two", "pwd", "echo four"]


def test_unmodelled_list_passes_untouched(ma):
    """A list the gate has no rule for must not have its items dropped.

    The gate answers "is this well-formed", not "is this allowed". If it
    started eating lists it cannot model, directives the legacy path handles
    would vanish with no trace.
    """
    items = [{"anything": 1}, {"other": 2}]
    kept, blocked = ma._validation_gate({"some_future_list": items})
    assert kept["some_future_list"] == items
    assert blocked == []


def test_empty_and_missing_inputs_are_safe(ma):
    kept, blocked = ma._validation_gate({"run_cmds": [], "read_paths": None})
    assert blocked == []
    assert kept["run_cmds"] == []


def test_gate_is_wired_into_process_reply(ma):
    """The gate must be called from the dispatch path, not just defined.

    Driven through a real process_reply() call rather than by reading its
    source: a getsource check proves the string "_validation_gate(" is
    somewhere in the file, which says nothing about whether it runs. It also
    broke under the full suite, where a second copy of master_ai can be
    imported and inspect resolves against the other one.
    """
    seen = {}

    def _spy(collected):
        seen["called"] = True
        return collected, []

    original = ma._validation_gate
    ma._validation_gate = _spy
    try:
        try:
            ma.process_reply(
                "Just a plain sentence with no directives at all.",
                [{"role": "user", "content": "hi"}],
            )
        except Exception:
            # process_reply may legitimately raise for other reasons; what
            # matters is whether the gate was reached.
            pass
    finally:
        ma._validation_gate = original

    assert seen.get("called") is True, "process_reply never reached the gate"


def test_blocked_feedback_precedes_first_dispatch_consumption(ma):
    """Ordering matters: the gate must run before anything executes.

    Asserted behaviourally. A reply whose only action is invalid must not
    reach execution, and must come back blocked rather than as a completed
    answer -- which is what would happen if the gate sat below a dispatch
    branch. A source-position check would only prove the current layout.
    """
    import master_ai

    executed = []
    original = master_ai.run_command
    master_ai.run_command = lambda cmd, *a, **k: executed.append(cmd)
    try:
        reply = "RUN: let me run the test suite"
        history = [{"role": "user", "content": "hi"}]
        try:
            result = master_ai.process_reply(reply, history)
        except Exception:
            result = "raised"

        assert executed == [], f"an invalid RUN reached execution: {executed}"
        assert result is None, "a fully-invalid reply must not be returned as an answer"
        blocked_msgs = [m for m in history if "[TOOL BLOCKED]" in str(m.get("content"))]
        assert blocked_msgs, "the model was not told the action was blocked"
    finally:
        master_ai.run_command = original


def test_blocked_feedback_invites_a_remember_line(ma):
    """The established contract: blocked feedback teaches the model.

    _append_tool_blocked_feedback's message ends by inviting a single
    `REMEMBER:` line, which is how the agent self-teaches from a refusal.
    The validation gate issues its own [TOOL BLOCKED], and must keep that
    invitation or the model loses the feedback loop on this path.
    """
    import master_ai

    history = [{"role": "user", "content": "hi"}]
    original = master_ai.run_command
    master_ai.run_command = lambda cmd, *a, **k: None
    try:
        try:
            master_ai.process_reply("RUN: let me run the test suite", history)
        except Exception:
            pass
    finally:
        master_ai.run_command = original

    blocked = [m for m in history if "[TOOL BLOCKED]" in str(m.get("content"))]
    assert blocked, "no [TOOL BLOCKED] feedback was recorded"
    assert "REMEMBER:" in blocked[-1]["content"], blocked[-1]["content"]


def test_validator_module_imports_standalone():
    """action_validation must not depend on master_ai (repo convention).

    Importing it in a fresh interpreter and asserting master_ai never enters
    sys.modules is the actual claim. Checking importability from an unrelated
    cwd would only be testing sys.path, which is not what this rule is about.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, action_validation as av;"
            "r = av.validate_action({'kind': 'RUN', 'target': 'ls -la'});"
            "assert r.ok;"
            "assert 'master_ai' not in sys.modules, 'validator pulled in master_ai';"
            "assert 'typed_actions' not in sys.modules, 'validator pulled in typed_actions';"
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
