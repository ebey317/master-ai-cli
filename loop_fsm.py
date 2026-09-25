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


class LoopState(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    AWAITING_TOOL_RESULT = "awaiting_tool_result"
    AWAITING_USER = "awaiting_user"
    DONE = "done"


class Event(str, Enum):
    USER_INPUT = "user_input"
    CONTINUE = "continue"
    MODEL_EMITTED_DIRECTIVE = "model_emitted_directive"
    MODEL_EMITTED_DONE = "model_emitted_done"
    MODEL_EMITTED_QUESTION = "model_emitted_question"
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
    (LoopState.PLANNING, Event.MODEL_EMITTED_QUESTION): LoopState.AWAITING_USER,
    (LoopState.EXECUTING, Event.TOOL_DISPATCHED): LoopState.AWAITING_TOOL_RESULT,
    (LoopState.AWAITING_TOOL_RESULT, Event.TOOL_RESULT): LoopState.PLANNING,
    (LoopState.AWAITING_TOOL_RESULT, Event.TERMINAL_RESULT): LoopState.DONE,
    (LoopState.AWAITING_TOOL_RESULT, Event.CONTINUE): LoopState.PLANNING,
    (LoopState.AWAITING_USER, Event.USER_INPUT): LoopState.PLANNING,
    (LoopState.DONE, Event.USER_INPUT): LoopState.PLANNING,
    (LoopState.DONE, Event.NEW_SESSION): LoopState.IDLE,
}


_MODEL_EMIT_EVENTS = {
    Event.MODEL_EMITTED_DIRECTIVE,
    Event.MODEL_EMITTED_DONE,
    Event.MODEL_EMITTED_QUESTION,
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
    turn_id: str | None = None
    parent_turn_id: str | None = None
    turn_count: int = 0
    turn_budget: int = 12
    terminal_reason: TerminalReason | None = None
    history: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    last_event_ts: float = field(default_factory=time.time)

    def transition(
        self,
        event: Event,
        *,
        reason: TerminalReason | None = None,
        turn_id: str | None = None,
        parent_turn_id: str | None = None,
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
            LoopState.AWAITING_USER,
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

        if (
            prev == LoopState.PLANNING
            and new_state == LoopState.EXECUTING
            and self.turn_count > self.turn_budget
        ):
            new_state = LoopState.DONE
            self.terminal_reason = TerminalReason.BUDGET_EXCEEDED

        self.state = new_state
        self.last_event_ts = time.time()
        self.history.append((prev.value, event.value, new_state.value, self.turn_count))
        return new_state

    @staticmethod
    def _infer_terminal_reason(event: Event) -> TerminalReason:
        if event == Event.MODEL_EMITTED_DONE:
            return TerminalReason.MODEL_DONE
        if event == Event.TERMINAL_RESULT:
            return TerminalReason.TOOL_TERMINAL
        return TerminalReason.NO_ACTIONS

    def force_terminal(self, reason: TerminalReason) -> None:
        prev = self.state
        self.state = LoopState.DONE
        self.terminal_reason = reason
        self.last_event_ts = time.time()
        self.history.append(
            (prev.value, "force_terminal", LoopState.DONE.value, self.turn_count)
        )

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
        return {
            "done": is_done,
            "active": is_active,
            "terminal_reason": terminal_reason_str,
            "terminal_authority": is_done,
            "state": self.state.value,
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
        "terminal_reason": (fsm.terminal_reason.value if fsm.terminal_reason else None),
        "turn_count": fsm.turn_count,
        "history_len": len(fsm.history),
    }


if __name__ == "__main__":
    import json
    import sys

    result = replay_17_turn_auto_fire()
    print(json.dumps(result, indent=2))

    ok = (
        result["fired"] == 1
        and result["refused"] == 17
        and result["final_state"] == LoopState.DONE.value
        and result["terminal_reason"] == TerminalReason.MODEL_DONE.value
    )
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
