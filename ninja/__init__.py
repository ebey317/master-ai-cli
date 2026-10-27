"""Ninja — Master AI desktop mascot / companion (Thootie).

Stage 3: Thootie states + waypoint drift + spawned approval card shell +
read-only queue poll + avatar-driven approval surface Tahuti can call.
"""

from ninja.avatar_approval import ask_avatar_approval, show_avatar_during_run
from ninja.card_box import ThootieCard, card_position, render_card_text
from ninja.queue_watch import entry_to_card, pending_first
from ninja.state_machine import (
    StatePayload,
    ThootieState,
    ThootieStateMachine,
    drift_step,
    park_idle,
    show_avatar,
)

__all__ = [
    "StatePayload",
    "ThootieCard",
    "ThootieState",
    "ThootieStateMachine",
    "ask_avatar_approval",
    "card_position",
    "drift_step",
    "entry_to_card",
    "park_idle",
    "pending_first",
    "render_card_text",
    "show_avatar",
    "show_avatar_during_run",
]
