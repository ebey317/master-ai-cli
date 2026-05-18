"""Loop FSM — server-authoritative loop termination contract.

Replaces the implicit state machine that was spread across side_panel.js
guards (`state.loop.last_done`, `state.loop.active`) and stt_server.py's
`_terminal_authority()` closure with one auditable typed state machine.

Six states, named transition table, RefusedTransition on illegal moves,
wire_view() collapses to the existing client guard vocab (done / active /
terminal_reason / terminal_authority) so integration into stt_server.py is
a field-by-field swap rather than a wire-format change.

Structural property the FSM enforces: CONTINUE from DONE is refused. The
17-turn ack-after-done auto-fire (extension auto-pumping /chat/continue
after a terminal turn) cannot reopen the loop server-side regardless of
what side_panel.js does. New operator turns (USER_INPUT) from DONE are
allowed; loop-pump continuations (CONTINUE) are not.

Stdlib-only. Importable from stt_server.py with no additional deps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LoopState(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    AWAITING_TOOL_RESULT = "awaiting_tool_result"
    AWAITING_INFO = "awaiting_info"
    AWAITING_CONFIRM = "awaiting_confirm"
    DONE = "done"


class Event(str, Enum):
    USER_INPUT = "user_input"
    CONTINUE = "continue"
    MODEL_EMITTED_DIRECTIVE = "model_emitted_directive"
    MODEL_EMITTED_DONE = "model_emitted_done"
    MODEL_EMITTED_QUESTION = "model_emitted_question"
    MODEL_EMITTED_PROPOSAL = "model_emitted_proposal"
    USER_APPROVED = "user_approved"
    USER_DECLINED = "user_declined"
    TOOL_DISPATCHED = "tool_dispatched"
    TOOL_RESULT = "tool_result"
    TERMINAL_RESULT = "terminal_result"
    NEW_SESSION = "new_session"


class TerminalReason(str, Enum):
    MODEL_DONE = "model_done"
    NO_ACTIONS = "no_actions"
    POLICY_REFUSAL = "policy_refusal"
    BUDGET_EXCEEDED = "budget_exceeded"
    TOOL_TERMINAL = "tool_terminal"
    OPERATOR_ABORT = "operator_abort"
    ACTION_LOOP_DETECTED = "action_loop_detected"


# Loop-detection window: when the same action signature repeats this many
# times within the last few EXECUTING transitions, the FSM force-terminates
# with ACTION_LOOP_DETECTED. Threshold of 3 matches Browser-Use's
# "if you are on the same URL for 3+ steps without meaningful progress,
# try a different approach." Adjust by changing FSM.loop_detect_threshold.
_DEFAULT_LOOP_DETECT_THRESHOLD = 3
_DEFAULT_LOOP_DETECT_WINDOW = 6


class RefusedTransition(Exception):
    def __init__(self, state: LoopState, event: Event, reason: str = ""):
        self.state = state
        self.event = event
        self.reason = reason
        super().__init__(
            f"refused transition from {state.value} on {event.value}"
            + (f": {reason}" if reason else "")
        )


_TRANSITIONS: dict[tuple[LoopState, Event], LoopState] = {
    (LoopState.IDLE, Event.USER_INPUT): LoopState.PLANNING,
    (LoopState.IDLE, Event.NEW_SESSION): LoopState.IDLE,

    (LoopState.PLANNING, Event.MODEL_EMITTED_DIRECTIVE): LoopState.EXECUTING,
    (LoopState.PLANNING, Event.MODEL_EMITTED_DONE): LoopState.DONE,
    (LoopState.PLANNING, Event.MODEL_EMITTED_QUESTION): LoopState.AWAITING_INFO,
    (LoopState.PLANNING, Event.MODEL_EMITTED_PROPOSAL): LoopState.AWAITING_CONFIRM,

    (LoopState.EXECUTING, Event.TOOL_DISPATCHED): LoopState.AWAITING_TOOL_RESULT,

    (LoopState.AWAITING_TOOL_RESULT, Event.TOOL_RESULT): LoopState.PLANNING,
    (LoopState.AWAITING_TOOL_RESULT, Event.TERMINAL_RESULT): LoopState.DONE,
    (LoopState.AWAITING_TOOL_RESULT, Event.CONTINUE): LoopState.PLANNING,

    # Hard block: model needs a value from the user. Resume by re-planning on
    # the next user message; CONTINUE pumps are refused (loop stays paused).
    (LoopState.AWAITING_INFO, Event.USER_INPUT): LoopState.PLANNING,

    # Soft block: model proposed an action, user confirms or declines.
    # USER_INPUT (a fresh operator turn, e.g. "wait do this other thing
    # instead") is also accepted — re-plans against the new ask. CONTINUE
    # pumps are refused (loop stays paused).
    (LoopState.AWAITING_CONFIRM, Event.USER_APPROVED): LoopState.EXECUTING,
    (LoopState.AWAITING_CONFIRM, Event.USER_DECLINED): LoopState.DONE,
    (LoopState.AWAITING_CONFIRM, Event.USER_INPUT): LoopState.PLANNING,

    (LoopState.DONE, Event.USER_INPUT): LoopState.PLANNING,
    (LoopState.DONE, Event.NEW_SESSION): LoopState.IDLE,
}


_MODEL_EMIT_EVENTS = {
    Event.MODEL_EMITTED_DIRECTIVE,
    Event.MODEL_EMITTED_DONE,
    Event.MODEL_EMITTED_QUESTION,
    Event.MODEL_EMITTED_PROPOSAL,
}


_AWAITING_STATES = {
    LoopState.AWAITING_INFO,
    LoopState.AWAITING_CONFIRM,
    LoopState.AWAITING_TOOL_RESULT,
}


_SESSION_OPENING_EVENTS = {Event.USER_INPUT, Event.NEW_SESSION}


_ACTIVE_STATES = {
    LoopState.PLANNING,
    LoopState.EXECUTING,
    LoopState.AWAITING_TOOL_RESULT,
}


@dataclass
class FSM:
    state: LoopState = LoopState.IDLE
    turn_id: Optional[str] = None
    parent_turn_id: Optional[str] = None
    turn_count: int = 0
    turn_budget: int = 12
    terminal_reason: Optional[TerminalReason] = None
    history: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    last_event_ts: float = field(default_factory=time.time)
    # Loop-detection ring buffer of recent action signatures. record_action()
    # pushes a signature on every EXECUTING transition; if the same
    # signature appears loop_detect_threshold times within the last
    # loop_detect_window entries, the FSM force-terminates with
    # ACTION_LOOP_DETECTED. Signatures should be the model's directive
    # text normalized (e.g. "BROWSER_NAV:https://indeed.com") so that
    # "navigate to indeed three times in a row" trips the detector
    # regardless of cosmetic whitespace.
    recent_actions: list = field(default_factory=list)
    loop_detect_threshold: int = _DEFAULT_LOOP_DETECT_THRESHOLD
    loop_detect_window: int = _DEFAULT_LOOP_DETECT_WINDOW

    def transition(
        self,
        event: Event,
        *,
        reason: Optional[TerminalReason] = None,
        turn_id: Optional[str] = None,
        parent_turn_id: Optional[str] = None,
    ) -> LoopState:
        key = (self.state, event)
        if key not in _TRANSITIONS:
            err = RefusedTransition(self.state, event)
            self.refused.append((self.state.value, event.value, time.time()))
            raise err

        prev = self.state
        new_state = _TRANSITIONS[key]

        if event in _SESSION_OPENING_EVENTS and prev in (
            LoopState.IDLE,
            LoopState.DONE,
            LoopState.AWAITING_INFO,
            LoopState.AWAITING_CONFIRM,
        ):
            self.turn_count = 0
            self.terminal_reason = None
            if turn_id is not None:
                self.turn_id = turn_id
            if parent_turn_id is not None:
                self.parent_turn_id = parent_turn_id

        if event in _MODEL_EMIT_EVENTS:
            self.turn_count += 1

        if new_state == LoopState.DONE:
            self.terminal_reason = reason or self._infer_terminal_reason(event)

        # USER_DECLINED out of AWAITING_CONFIRM lands in DONE; record the
        # operator-abort reason unless caller supplied an explicit one.
        if event == Event.USER_DECLINED and new_state == LoopState.DONE:
            self.terminal_reason = reason or TerminalReason.OPERATOR_ABORT

        if (
            prev == LoopState.PLANNING
            and new_state == LoopState.EXECUTING
            and self.turn_count > self.turn_budget
        ):
            new_state = LoopState.DONE
            self.terminal_reason = TerminalReason.BUDGET_EXCEEDED

        self.state = new_state
        self.last_event_ts = time.time()
        self.history.append(
            (prev.value, event.value, new_state.value, self.turn_count)
        )
        return new_state

    @staticmethod
    def _infer_terminal_reason(event: Event) -> TerminalReason:
        if event == Event.MODEL_EMITTED_DONE:
            return TerminalReason.MODEL_DONE
        if event == Event.TERMINAL_RESULT:
            return TerminalReason.TOOL_TERMINAL
        if event == Event.USER_DECLINED:
            return TerminalReason.OPERATOR_ABORT
        return TerminalReason.NO_ACTIONS

    def force_terminal(self, reason: TerminalReason) -> None:
        prev = self.state
        self.state = LoopState.DONE
        self.terminal_reason = reason
        self.last_event_ts = time.time()
        self.history.append(
            (prev.value, "force_terminal", LoopState.DONE.value, self.turn_count)
        )

    def record_action(self, signature: str) -> Optional[TerminalReason]:
        """Push a normalized action signature into the ring buffer; if the
        same signature has appeared loop_detect_threshold times within the
        last loop_detect_window entries, force-terminate the FSM with
        ACTION_LOOP_DETECTED and return the reason. Otherwise return None.

        Caller should compute the signature from the model's directive text
        (e.g. f"{token}:{target}" with whitespace collapsed, lowercased).
        That keeps "BROWSER_NAV: https://x" and " browser_nav:https://x "
        from looking like distinct actions on cosmetic differences.
        """
        self.recent_actions.append(signature)
        # Trim ring buffer to the detection window. Older entries cannot
        # contribute to a fresh loop and just bloat memory on long sessions.
        if len(self.recent_actions) > self.loop_detect_window:
            self.recent_actions = self.recent_actions[-self.loop_detect_window:]

        # Count occurrences within the window.
        recent_count = self.recent_actions.count(signature)
        if recent_count >= self.loop_detect_threshold:
            self.force_terminal(TerminalReason.ACTION_LOOP_DETECTED)
            return TerminalReason.ACTION_LOOP_DETECTED
        return None

    def reset_session(self) -> None:
        self.state = LoopState.IDLE
        self.turn_id = None
        self.parent_turn_id = None
        self.turn_count = 0
        self.terminal_reason = None
        self.last_event_ts = time.time()
        self.history.append(("*", "reset_session", LoopState.IDLE.value, 0))

    def wire_view(self) -> dict:
        is_done = self.state == LoopState.DONE
        is_active = self.state in _ACTIVE_STATES
        terminal_reason_str = (
            self.terminal_reason.value
            if (is_done and self.terminal_reason is not None)
            else None
        )
        # awaiting_kind exposes which kind of pause the loop is in:
        # "info" = hard block, model asked the user a question and the loop
        # cannot continue until the user supplies a value;
        # "confirm" = soft block, model proposed an action and is waiting on
        # USER_APPROVED / USER_DECLINED (the client-side approval dock).
        # A well-formed run-prompt that pre-answers clarifying questions
        # should clear "confirm" gates without round-tripping back through
        # the operator; "info" gates always halt until the operator responds.
        if self.state == LoopState.AWAITING_INFO:
            awaiting_kind = "info"
        elif self.state == LoopState.AWAITING_CONFIRM:
            awaiting_kind = "confirm"
        else:
            awaiting_kind = None
        return {
            "done": is_done,
            "active": is_active,
            "terminal_reason": terminal_reason_str,
            "terminal_authority": is_done,
            "state": self.state.value,
            "awaiting_kind": awaiting_kind,
            "turn_id": self.turn_id,
            "parent_turn_id": self.parent_turn_id,
            "turn_count": self.turn_count,
            "turn_budget": self.turn_budget,
        }


def replay_17_turn_auto_fire() -> dict:
    fsm = FSM()
    fsm.transition(Event.USER_INPUT, turn_id="t0", parent_turn_id=None)
    fsm.transition(Event.MODEL_EMITTED_DIRECTIVE)
    fsm.transition(Event.TOOL_DISPATCHED)
    fsm.transition(Event.TOOL_RESULT)
    fsm.transition(Event.MODEL_EMITTED_DONE)

    fired = 1
    refused = 0
    for _ in range(17):
        try:
            fsm.transition(Event.CONTINUE)
            fired += 1
        except RefusedTransition:
            refused += 1

    return {
        "fired": fired,
        "refused": refused,
        "final_state": fsm.state.value,
        "terminal_reason": (
            fsm.terminal_reason.value if fsm.terminal_reason else None
        ),
        "turn_count": fsm.turn_count,
        "history_len": len(fsm.history),
    }


def replay_awaiting_info_holds_against_continue() -> dict:
    """Hard block: model asks a clarifying question (MODEL_EMITTED_QUESTION),
    FSM lands in AWAITING_INFO, and 17 loop-pump CONTINUE events are refused
    until USER_INPUT arrives. Mirrors the 17-turn auto-fire test but for the
    info gate instead of the DONE gate."""
    fsm = FSM()
    fsm.transition(Event.USER_INPUT, turn_id="t1", parent_turn_id=None)
    fsm.transition(Event.MODEL_EMITTED_QUESTION)

    refused = 0
    for _ in range(17):
        try:
            fsm.transition(Event.CONTINUE)
        except RefusedTransition:
            refused += 1

    state_before_user = fsm.state.value
    awaiting_kind_before = fsm.wire_view()["awaiting_kind"]

    fsm.transition(Event.USER_INPUT)
    state_after_user = fsm.state.value

    return {
        "state_before_user": state_before_user,
        "awaiting_kind_before": awaiting_kind_before,
        "refused": refused,
        "state_after_user": state_after_user,
    }


def replay_awaiting_confirm_user_approved() -> dict:
    """Soft block: model proposes an action (MODEL_EMITTED_PROPOSAL), FSM
    lands in AWAITING_CONFIRM, user clicks Allow → USER_APPROVED → EXECUTING.
    CONTINUE pumps during the pause are refused."""
    fsm = FSM()
    fsm.transition(Event.USER_INPUT, turn_id="t2", parent_turn_id=None)
    fsm.transition(Event.MODEL_EMITTED_PROPOSAL)

    awaiting_kind = fsm.wire_view()["awaiting_kind"]

    refused = 0
    for _ in range(3):
        try:
            fsm.transition(Event.CONTINUE)
        except RefusedTransition:
            refused += 1

    fsm.transition(Event.USER_APPROVED)
    return {
        "awaiting_kind": awaiting_kind,
        "refused": refused,
        "final_state": fsm.state.value,
    }


def replay_action_loop_detected() -> dict:
    """Loop detection: the same action signature repeated 3 times within
    the detection window force-terminates with ACTION_LOOP_DETECTED."""
    fsm = FSM()
    fsm.transition(Event.USER_INPUT, turn_id="t4", parent_turn_id=None)

    triggered_reason = None
    for i in range(5):
        # First two emits don't fire; third should trip the detector.
        # We invoke record_action manually because in production it would
        # be called from the directive parser on each EXECUTING transition.
        fsm.transition(Event.MODEL_EMITTED_DIRECTIVE)
        triggered_reason = fsm.record_action("browser_nav:https://indeed.com")
        if triggered_reason is not None:
            break
        fsm.transition(Event.TOOL_DISPATCHED)
        fsm.transition(Event.TOOL_RESULT)

    return {
        "iterations_until_trip": i + 1,
        "final_state": fsm.state.value,
        "terminal_reason": (
            fsm.terminal_reason.value if fsm.terminal_reason else None
        ),
        "triggered_reason": triggered_reason.value if triggered_reason else None,
    }


def replay_action_loop_not_triggered() -> dict:
    """Three different actions in a row do NOT trigger the loop detector."""
    fsm = FSM()
    fsm.transition(Event.USER_INPUT, turn_id="t5", parent_turn_id=None)
    fsm.transition(Event.MODEL_EMITTED_DIRECTIVE)
    r1 = fsm.record_action("browser_nav:https://a.com")
    fsm.transition(Event.TOOL_DISPATCHED)
    fsm.transition(Event.TOOL_RESULT)
    fsm.transition(Event.MODEL_EMITTED_DIRECTIVE)
    r2 = fsm.record_action("browser_nav:https://b.com")
    fsm.transition(Event.TOOL_DISPATCHED)
    fsm.transition(Event.TOOL_RESULT)
    fsm.transition(Event.MODEL_EMITTED_DIRECTIVE)
    r3 = fsm.record_action("browser_nav:https://c.com")
    return {
        "trips": [r1, r2, r3],
        "final_state": fsm.state.value,
    }


def replay_awaiting_confirm_user_declined() -> dict:
    """Soft block decline path: AWAITING_CONFIRM → USER_DECLINED → DONE with
    terminal_reason=OPERATOR_ABORT."""
    fsm = FSM()
    fsm.transition(Event.USER_INPUT, turn_id="t3", parent_turn_id=None)
    fsm.transition(Event.MODEL_EMITTED_PROPOSAL)
    fsm.transition(Event.USER_DECLINED)
    return {
        "final_state": fsm.state.value,
        "terminal_reason": (
            fsm.terminal_reason.value if fsm.terminal_reason else None
        ),
    }


if __name__ == "__main__":
    import json
    import sys

    auto_fire = replay_17_turn_auto_fire()
    info_hold = replay_awaiting_info_holds_against_continue()
    confirm_ok = replay_awaiting_confirm_user_approved()
    confirm_no = replay_awaiting_confirm_user_declined()
    loop_trip = replay_action_loop_detected()
    loop_safe = replay_action_loop_not_triggered()

    bundle = {
        "auto_fire": auto_fire,
        "info_hold": info_hold,
        "confirm_ok": confirm_ok,
        "confirm_no": confirm_no,
        "loop_trip": loop_trip,
        "loop_safe": loop_safe,
    }
    print(json.dumps(bundle, indent=2))

    ok = (
        auto_fire["fired"] == 1
        and auto_fire["refused"] == 17
        and auto_fire["final_state"] == LoopState.DONE.value
        and auto_fire["terminal_reason"] == TerminalReason.MODEL_DONE.value
        and info_hold["state_before_user"] == LoopState.AWAITING_INFO.value
        and info_hold["awaiting_kind_before"] == "info"
        and info_hold["refused"] == 17
        and info_hold["state_after_user"] == LoopState.PLANNING.value
        and confirm_ok["awaiting_kind"] == "confirm"
        and confirm_ok["refused"] == 3
        and confirm_ok["final_state"] == LoopState.EXECUTING.value
        and confirm_no["final_state"] == LoopState.DONE.value
        and confirm_no["terminal_reason"] == TerminalReason.OPERATOR_ABORT.value
        and loop_trip["triggered_reason"] == TerminalReason.ACTION_LOOP_DETECTED.value
        and loop_trip["final_state"] == LoopState.DONE.value
        and loop_safe["trips"] == [None, None, None]
    )
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
