"""
Reasoning effort selection for model pickers.
Portable design: every model pick carries its reasoning level; applied after swap with pick's scope.
Capability-gated: hidden when the route/model lacks reasoning control.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from sensei import config  # type: ignore[attr-defined]  # Sensei's config module


class ReasoningEffort(str, Enum):
    """Valid reasoning effort levels."""
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "xhigh"  # matches upstream "xhigh"

    @classmethod
    def valid_values(cls) -> list[str]:
        return [e.value for e in cls]

    @classmethod
    def parse(cls, value: str) -> "ReasoningEffort":
        """Parse and validate reasoning effort, normalizing unicode dashes."""
        normalized = value.replace("\u2013", "-").replace("\u2014", "-").strip().lower()
        for effort in cls:
            if effort.value == normalized:
                return effort
        raise ValueError(f"Invalid reasoning effort: {value}. Valid: {cls.valid_values()}")


@dataclass
class ModelSwitchRequest:
    """Request to switch model with optional reasoning effort."""
    model: str
    provider: Optional[str] = None
    reasoning_effort: Optional[ReasoningEffort] = None
    scope: str = "session"  # "session" | "global" | "once"
    explicit_provider: bool = False


@dataclass
class ModelPickerState:
    """State for multi-stage model picker."""
    stage: int = 1  # 1=provider, 2=model, 3=reasoning
    selected_provider: Optional[str] = None
    selected_model: Optional[str] = None
    selected_reasoning: Optional[ReasoningEffort] = None
    available_providers: list[str] = field(default_factory=list)
    available_models: list[dict] = field(default_factory=list)
    supports_reasoning: bool = True


def add_reasoning_arg(parser: argparse.ArgumentParser) -> None:
    """Add --reasoning argument to a model command parser."""
    parser.add_argument(
        "--reasoning",
        choices=ReasoningEffort.valid_values() + ["none"],
        help="Reasoning effort level for the model (default: keep current)",
    )


def apply_reasoning_effort(
    session_key: str,
    effort: ReasoningEffort,
    scope: str = "session",
    persist_global: bool = False,
) -> str:
    """
    Apply reasoning effort with the given scope.
    Returns confirmation message.
    """
    if scope == "global" or persist_global:
        config.set("agent.reasoning_effort", effort.value)
        return f"Reasoning effort: {effort.value} (global)"
    elif scope == "once":
        # Snapshot current, apply for one turn, restore after
        current = config.get("agent.reasoning_effort", "medium")
        config.set("session.reasoning_override", effort.value)
        config.set("session.reasoning_restore", current)
        return f"Reasoning effort: {effort.value} (one turn)"
    else:  # session
        config.set("session.reasoning_override", effort.value)
        return f"Reasoning effort: {effort.value} (session)"


def build_reasoning_picker_rows(current: Optional[str] = None) -> list[tuple[str, str]]:
    """Build (value, label) rows for reasoning effort picker."""
    rows = [
        ("none", "None (disable reasoning)"),
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("xhigh", "Very High"),
    ]
    if current:
        rows.append((current, f"Keep current ({current})"))
    return rows


def model_supports_reasoning(model_info: dict) -> bool:
    """Check if a model/route supports reasoning control via capability map."""
    return model_info.get("supports_reasoning", True)


async def run_model_picker_with_reasoning(
    initial_request: ModelSwitchRequest,
) -> ModelSwitchRequest:
    """
    Run interactive model picker with reasoning effort stage.
    Returns completed request with model, provider, and reasoning_effort.
    """
    state = ModelPickerState()
    request = initial_request

    # Stage 1: Provider selection (if not explicit)
    if not request.explicit_provider and not request.provider:
        # ... provider selection logic ...
        pass

    # Stage 2: Model selection
    if not request.model:
        # ... model selection logic ...
        # Check capability
        model_info = next((m for m in state.available_models if m["name"] == request.model), {})
        state.supports_reasoning = model_supports_reasoning(model_info)

    # Stage 3: Reasoning effort (only if supported)
    if state.supports_reasoning and request.reasoning_effort is None:
        rows = build_reasoning_picker_rows(
            config.get("session.reasoning_override") or config.get("agent.reasoning_effort")
        )
        # ... picker UI logic ...
        # request.reasoning_effort = ReasoningEffort.parse(selected_value)

    return request
