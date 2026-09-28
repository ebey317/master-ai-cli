"""End-to-end typed dispatch validation (CLAUDE.md Tier-1, item 4).

The Tier-1 requirement: "all model outputs validate before ANY dispatch".
These tests run the real parser (`typed_actions.parse_reply_with_bodies`)
into the real pre-dispatch gate (`action_validation.validate_all`) over
realistic model replies, and assert on what would be dispatched versus what
gets reported back to the model as blocked.

The failure this exists to prevent is specific: a model claims a step it
never performed, because a malformed or unparseable directive was dispatched
anyway (or silently dropped) and the chain continued regardless. So the
assertions here are about the BLOCKED set being non-empty and correctly
attributed -- not merely about the validator returning something.

No master_ai import: the gate is standalone by design, and process_reply's
wiring is covered by test_typed_dispatch_wiring.py.
"""

from __future__ import annotations

import pytest

import action_validation as av
import typed_actions as ta


def parse_and_validate(reply: str):
    """The full pre-dispatch path: model text -> parsed actions -> gate."""
    actions = ta.parse_reply_with_bodies(reply, model="cloud_fast", cwd="/tmp")
    dispatchable, blocked = av.validate_all(actions)
    return actions, dispatchable, blocked


def blocked_kinds(blocked) -> set:
    return {a.kind for a, _r in blocked}


# ── valid replies dispatch in full ──


def test_real_command_dispatches():
    _a, dispatchable, blocked = parse_and_validate("RUN: ls -la /tmp")
    assert len(dispatchable) == 1
    assert blocked == []


def test_chained_commands_dispatch():
    _a, dispatchable, blocked = parse_and_validate("RUN: cd /tmp && ls -la")
    assert len(dispatchable) == 1 and blocked == []


def test_quoted_path_dispatches():
    _a, dispatchable, blocked = parse_and_validate('RUN: cat "/tmp/my file.txt"')
    assert len(dispatchable) == 1 and blocked == []


def test_read_dispatches():
    _a, dispatchable, blocked = parse_and_validate("READ: /tmp/notes.md")
    assert len(dispatchable) == 1 and blocked == []


def test_valid_python_create_dispatches():
    reply = "CREATE: /tmp/ok.py\n<<<CONTENT\ndef f():\n    return 1\n>>>CONTENT"
    _a, dispatchable, blocked = parse_and_validate(reply)
    assert len(dispatchable) == 1 and blocked == []


def test_valid_shell_create_dispatches():
    reply = "CREATE: /tmp/ok.sh\n<<<CONTENT\n#!/bin/bash\nset -euo pipefail\necho hi\n>>>CONTENT"
    _a, dispatchable, blocked = parse_and_validate(reply)
    assert len(dispatchable) == 1 and blocked == []


def test_valid_json_create_dispatches():
    reply = 'CREATE: /tmp/ok.json\n<<<CONTENT\n{"a": 1}\n>>>CONTENT'
    _a, dispatchable, blocked = parse_and_validate(reply)
    assert len(dispatchable) == 1 and blocked == []


# ── malformed replies are blocked, and said why ──


def test_unparseable_python_create_is_blocked():
    reply = "CREATE: /tmp/bad.py\n<<<CONTENT\ndef broken(\n>>>CONTENT"
    _a, dispatchable, blocked = parse_and_validate(reply)
    assert dispatchable == [], "a file that cannot parse must not be written"
    assert "CREATE" in blocked_kinds(blocked)
    reason = blocked[0][1].reason
    assert "syntax error" in reason, reason
    assert "line" in reason, "the reason must point at the line for the model to fix it"


def test_unparseable_shell_create_is_blocked():
    reply = "CREATE: /tmp/bad.sh\n<<<CONTENT\n#!/bin/bash\nif [ -f x ; then\n>>>CONTENT"
    _a, dispatchable, blocked = parse_and_validate(reply)
    assert dispatchable == []
    assert "shell syntax error" in blocked[0][1].reason


def test_invalid_json_create_is_blocked():
    reply = "CREATE: /tmp/bad.json\n<<<CONTENT\n{not json,,}\n>>>CONTENT"
    _a, dispatchable, blocked = parse_and_validate(reply)
    assert dispatchable == []
    assert "json syntax error" in blocked[0][1].reason


def test_narrated_run_is_blocked():
    """The headline case: a model describing an action instead of issuing it.

    Without this the dispatcher execs `let` and the model goes on to report
    the step as done.
    """
    _a, dispatchable, blocked = parse_and_validate("RUN: let me run the test suite")
    assert dispatchable == []
    assert "sentence" in blocked[0][1].reason


def test_unbalanced_quoting_is_blocked():
    _a, dispatchable, blocked = parse_and_validate('RUN: echo "unterminated')
    assert dispatchable == []
    assert "quoting" in blocked[0][1].reason


def test_bad_runterm_shell_is_blocked():
    _a, dispatchable, blocked = parse_and_validate("RUNTERM: if [ -f x ; then echo hi")
    assert dispatchable == []
    assert "shell syntax error" in blocked[0][1].reason


def test_empty_payload_is_blocked():
    """A directive with nothing after the colon dispatches nothing."""
    actions = ta.parse_reply_with_bodies("RUN:", model="m", cwd="/tmp")
    if not actions:
        pytest.skip("parser drops a bare RUN: before validation sees it")
    dispatchable, blocked = av.validate_all(actions)
    assert dispatchable == []
    assert "empty" in blocked[0][1].reason


# ── a mixed reply: the good parts still run, the bad part is reported ──


def test_mixed_reply_dispatches_good_and_blocks_bad():
    """The realistic case, and the one the Tier-1 gate exists for.

    A reply that writes a broken file and then tries to run something else
    must not have the broken write land. The independent action still runs.
    """
    reply = (
        "I'll write the module then run it.\n"
        "CREATE: /tmp/broken.py\n"
        "<<<CONTENT\n"
        "def oops(:\n"
        ">>>CONTENT\n"
        "RUN: pytest -q\n"
    )
    _a, dispatchable, blocked = parse_and_validate(reply)

    kinds_dispatched = [a.kind for a in dispatchable]
    assert "CREATE" not in kinds_dispatched, "the unparseable file must not be written"
    assert "RUN" in kinds_dispatched, "an independent valid action should still run"
    assert "CREATE" in blocked_kinds(blocked)
    assert len(blocked) == 1, "exactly the bad action should be reported"


def test_blocked_reason_is_written_for_the_model():
    """Reasons go back into history, so they must be actionable prose."""
    _a, _d, blocked = parse_and_validate(
        "CREATE: /tmp/x.py\n<<<CONTENT\ndef f(\n>>>CONTENT"
    )
    reason = blocked[0][1].reason
    assert reason and not reason.startswith("ValidationResult")
    assert "CREATE" in reason or "parse" in reason


# ── the gate never raises, and never blocks what it cannot model ──


def test_validator_never_raises_on_junk():
    for junk in [None, "", 0, [], {}, {"kind": None}, object()]:
        result = av.validate_action(junk)
        assert result.ok in (True, False)


def test_unmodelled_kinds_pass_through():
    """An unknown kind must not be silently eaten.

    The gate answers "is this well-formed", not "is this allowed". Refusing
    kinds it has no rule for would drop directives the legacy path handles.
    """
    for kind in ("BROWSER_NAV", "SEARCH", "REMEMBER", "PLAN", "SEND_EMAIL"):
        assert av.validate_action({"kind": kind, "target": "x"}).ok is True


def test_dict_and_object_actions_validate_the_same():
    """Callers hold TypedAction objects; the gate also accepts plain dicts."""
    obj = ta.TypedAction(kind="RUN", target="ls -la /tmp")
    assert (
        av.validate_action(obj).ok
        == av.validate_action({"kind": "RUN", "target": "ls -la /tmp"}).ok
    )


def test_order_is_preserved():
    reply = "RUN: ls\nRUNTERM: echo hi\nREAD: /tmp/x"
    _a, dispatchable, _b = parse_and_validate(reply)
    assert [a.kind for a in dispatchable] == ["RUN", "RUNTERM", "READ"]
