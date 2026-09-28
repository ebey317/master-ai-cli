"""Tests for the second review pass on the parsing-baseline rescue.

Three fixes, each found by `ocr review` on the rescue commits:

1. `fallback add` / `fallback remove` / `fallback move` compared the
   operator's typed name against a lowercase allowlist without normalising
   case, so `fallback add NVIDIA` reported "unknown provider 'NVIDIA'".

2. main()'s reload-carry restore called read_text() and only then unlink(),
   so an unreadable carry file survived and re-logged
   AUTO_RELOAD_CARRY_RESTORE_ERROR on every startup.

3. Two `except NotImplementedError` guards were stub-era scaffolding left
   behind once the guarded predicates were implemented.

(1) is exercised through the real _handle_fallback_cmd() seam, which this
work extracted out of main()'s inline if-chain precisely so it could be
tested without driving the whole REPL. (3) exercises the two predicates
directly. (2) has no seam -- it is a five-line startup block inside main()
-- so it is verified in test_auto_reload_carry.py against the same block's
observable contract rather than a re-implementation of it.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ma(monkeypatch):
    import master_ai

    monkeypatch.setattr(master_ai, "log", lambda *a, **k: None)
    return master_ai


@pytest.fixture
def chain(ma, monkeypatch, tmp_path):
    """Serve a fixed fallback chain and capture whatever gets saved."""
    state = {"order": ["nemotron", "nvidia"], "saved": []}
    monkeypatch.setattr(
        ma, "_load_fallback_order", lambda: list(state["order"]), raising=False
    )
    monkeypatch.setattr(
        ma,
        "_save_fallback_order",
        lambda names: state["saved"].append(list(names)),
        raising=False,
    )
    monkeypatch.setattr(ma, "_FALLBACK_ORDER_FILE", tmp_path / "fb.json", raising=False)
    return state


def _run(ma, capsys, text):
    """Invoke the real handler and return what it printed."""
    assert ma._handle_fallback_cmd(text.strip().lower(), text.strip()) is True
    return capsys.readouterr().out


# ── 1. fallback name case-insensitivity ──


def test_fallback_add_accepts_uppercase_name(ma, chain, capsys):
    # "opencode" is not in the fixture chain, so this exercises a real add
    # rather than the duplicate guard.
    out = _run(ma, capsys, "fallback add OPENCODE")
    assert "unknown provider" not in out, out
    assert chain["saved"] == [["nemotron", "nvidia", "opencode"]], chain


def test_fallback_move_accepts_uppercase_name(ma, chain, capsys):
    out = _run(ma, capsys, "fallback move NVIDIA 1")
    assert "isn't in the current chain" not in out, out
    assert chain["saved"] == [["nvidia", "nemotron"]], chain


def test_fallback_remove_accepts_uppercase_name(ma, chain, capsys):
    out = _run(ma, capsys, "fallback remove NVIDIA")
    assert "isn't in the current chain" not in out, out
    assert chain["saved"] == [["nemotron"]], chain


def test_fallback_move_moves_to_the_middle(ma, chain, capsys):
    chain["order"] = ["a-none", "nemotron", "nvidia", "deepseek-r1"]
    out = _run(ma, capsys, "fallback move deepseek-r1 2")
    assert chain["saved"] == [["a-none", "deepseek-r1", "nemotron", "nvidia"]], chain


def test_fallback_move_rejects_unknown_name(ma, chain, capsys):
    out = _run(ma, capsys, "fallback move notaprovider 1")
    assert "isn't in the current chain" in out, out
    assert chain["saved"] == [], "must not save on a rejected name"


def test_fallback_move_rejects_out_of_range_position(ma, chain, capsys):
    out = _run(ma, capsys, "fallback move nvidia 9")
    assert "usage: fallback move" in out, out
    assert chain["saved"] == [], "must not save on a bad position"


def test_fallback_move_rejects_zero_position(ma, chain, capsys):
    out = _run(ma, capsys, "fallback move nvidia 0")
    assert "usage: fallback move" in out, out
    assert chain["saved"] == []


def test_fallback_move_rejects_missing_position(ma, chain, capsys):
    out = _run(ma, capsys, "fallback move nvidia")
    assert "usage: fallback move" in out, out
    assert chain["saved"] == []


def test_fallback_add_rejects_unknown_provider(ma, chain, capsys):
    out = _run(ma, capsys, "fallback add OPENCODE")
    assert "unknown provider" not in out, out
    out = _run(ma, capsys, "fallback add notaprovider")
    assert "unknown provider" in out, out
    assert chain["saved"] == [["nemotron", "nvidia", "opencode"]]


def test_fallback_add_refuses_duplicate(ma, chain, capsys):
    out = _run(ma, capsys, "fallback add NEMOTRON")
    assert "already in the chain" in out, out
    assert chain["saved"] == [], "duplicate add must not rewrite the chain"


def test_fallback_list_and_reset(ma, chain, capsys):
    out = _run(ma, capsys, "fallback list")
    assert "Cloud fallback chain" in out, out
    assert "nemotron" in out and "nvidia" in out
    assert "fallback move" in out, "list must advertise the move subcommand"

    out = _run(ma, capsys, "fallback reset")
    assert "reset to default chain" in out, out


def test_non_fallback_command_is_not_consumed(ma):
    assert ma._handle_fallback_cmd("doctor", "doctor") is False
    assert ma._handle_fallback_cmd("fallbackish", "fallbackish") is False


# ── 3. no stub-era NotImplementedError guards remain ──


def test_predicates_are_implemented_and_pure(ma):
    assert ma._reply_needs_operator_input("all done") is False
    assert ma._reply_needs_operator_input("shall I?") is True
    assert ma._reply_claims_unexecuted_action("executing the checks now") is True
    assert ma._reply_claims_unexecuted_action("Here is the answer.") is False
