"""Thootie stage-2: states, corners, drift, spawned-box math, queue poll."""

import json

from ninja.card_box import card_position, render_card_text
from ninja.queue_watch import entry_to_card, pending_first
from ninja.state_machine import (
    _EXCLUDED_NOTIFICATION_CORNER,
    _WAYPOINTS,
    StatePayload,
    ThootieState,
    ThootieStateMachine,
    drift_step,
)


def test_machine_starts_idle():
    m = ThootieStateMachine()
    assert m.state == ThootieState.IDLE
    assert m.describe() == "idle (idle)"


def test_set_state_fires_hooks_and_payload():
    seen = []
    m = ThootieStateMachine(
        redraw_hook=lambda: seen.append("redraw"),
        state_change_hook=lambda o, n: seen.append((o, n)),
    )
    m.set_state(ThootieState.APPROVAL_INCOMING, StatePayload(action_kind="grant"))
    assert m.state == ThootieState.APPROVAL_INCOMING
    assert (ThootieState.IDLE, ThootieState.APPROVAL_INCOMING) in seen
    assert "redraw" in seen
    assert m.describe() == "approval-incoming (grant)"


def test_eight_thootie_states():
    assert [s.value for s in ThootieState] == [
        "idle",
        "working",
        "sorting",
        "approval-incoming",
        "approval-presented",
        "approved-run",
        "denied",
        "edit",
    ]


def test_waypoints_exclude_notification_corner():
    assert _EXCLUDED_NOTIFICATION_CORNER not in _WAYPOINTS
    assert _WAYPOINTS == [(0.02, 0.02), (0.02, 0.98), (0.98, 0.98)]


def test_drift_step_converges():
    pos = (0.0, 0.0)
    for _ in range(200):
        pos = drift_step(pos, (100.0, 50.0))
    assert abs(pos[0] - 100.0) < 1.0
    assert abs(pos[1] - 50.0) < 1.0


def test_drift_step_full_alpha_jumps():
    assert drift_step((0.0, 0.0), (5.0, 5.0), alpha=1.0) == (5.0, 5.0)


def test_drift_step_rejects_bad_alpha():
    for bad in (0.0, -0.5, 1.5):
        try:
            drift_step((0.0, 0.0), (1.0, 1.0), alpha=bad)
        except ValueError:
            continue
        raise AssertionError(f"alpha={bad} should raise")


def test_card_position_left_corners_spawn_right():
    x, y = card_position(44, 20, 220, "top-left", 340, 300, 1920, 1080)
    assert (x, y) == (44 + 220 + 12, 20)
    x, y = card_position(44, 700, 220, "bottom-left", 340, 300, 1920, 1080)
    assert (x, y) == (44 + 220 + 12, 700)


def test_card_position_bottom_right_spawns_left():
    x, y = card_position(1656, 700, 220, "bottom-right", 340, 300, 1920, 1080)
    assert (x, y) == (1656 - 12 - 340, 700)


def test_card_position_clamps_on_screen():
    x, y = card_position(10, 900, 220, "top-left", 340, 300, 800, 1000)
    assert 0 <= x <= 800 - 340
    assert 0 <= y <= 1000 - 300


def test_render_card_text_carries_full_card():
    text = render_card_text(
        {
            "question": "Want me to run the suite?",
            "who": "Master AI",
            "what": "pytest",
            "where": "repo",
            "when": "now",
            "why": "red?",
            "how": "auto",
            "executor": "Master AI — handler repo_test_check",
            "model": "deterministic code — no model, $0",
        }
    )
    for needle in (
        "QUESTION: Want me to run the suite?",
        "Who: Master AI",
        "What: pytest",
        "Where: repo",
        "When: now",
        "Why: red?",
        "How: auto",
        "Executor: Master AI — handler repo_test_check",
        "Model: deterministic code — no model, $0",
    ):
        assert needle in text


def test_pending_first_skips_noise(tmp_path):
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        "\n".join(
            [
                "",
                "{not json",
                json.dumps({"id": "a", "status": "DONE"}),
                json.dumps({"id": "b", "status": "PENDING", "what": "first"}),
                json.dumps({"id": "c", "status": "PENDING", "what": "second"}),
            ]
        )
    )
    entry = pending_first(queue)
    assert entry is not None and entry["id"] == "b"


def test_pending_first_missing_file_is_none(tmp_path):
    assert pending_first(tmp_path / "absent.jsonl") is None


def test_entry_to_card_mapping_and_defaults():
    card = entry_to_card(
        {
            "type": "repo_test_check",
            "who": "Master AI",
            "what": "run suite",
            "where": "r",
            "when": "now",
            "why": "y",
            "how": "h",
            "payload": {},
        }
    )
    assert card["question"] == "run suite"
    assert card["executor"] == "Master AI — handler repo_test_check"
    assert card["model"] == "deterministic code — no model, $0"
    card2 = entry_to_card({"type": "x", "payload": {"question": "Q?", "model": "m-1"}})
    assert card2["question"] == "Q?"
    assert card2["model"] == "m-1"
