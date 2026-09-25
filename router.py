"""Routing surface for Master AI.

Compatibility boundary, not a rewrite. The routing decision logic still lives
in ``master_ai.orchestrate()`` and ``master_ai.detect_route()``; this module
exposes them as a small typed surface so callers (typed actions in P0.4,
Pupil, observability) can depend on a stable shape instead of master_ai's
globals.

Public API:
    route(history, user_text, image_path=None) -> dict
        Thin wrapper around master_ai.orchestrate(). Returns the same dict
        shape master_ai uses today. Always present keys: 'route', 'reason'.
        Optional keys (per route): 'model', 'stripped_text', 'synth_reply',
        'question', 'payload', 'response', 'similarity', 'source_model',
        'original_query', 'have_groq', 'have_or', 'query'.

    detect(text, has_image=False) -> (route, model, reason)
        Pass-through to master_ai.detect_route().

    RouteDecision.from_dict(d) -> RouteDecision
        Dataclass mirror for callers that prefer attribute access. Unknown
        keys land in ``extras`` so future master_ai additions don't break
        the wrapper.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402

__all__ = ["route", "detect", "RouteDecision"]


_KNOWN_ROUTES = frozenset(
    {
        "local",
        "cloud",
        "cloud_fast",
        "cloud_deep",
        "cloud_vision",
        "vision",
        "web",
        "weather",
        "system_query",
        "link_lookup",
        "time_sensitive_warn",
        "recall_memory",
        "save_refresh",
        "ask_user",
        "scope_check",
        "cached",
    }
)

_KNOWN_KEYS = frozenset(
    {
        "route",
        "model",
        "reason",
        "stripped_text",
        "synth_reply",
        "question",
        "payload",
        "response",
        "similarity",
        "source_model",
        "original_query",
        "have_groq",
        "have_or",
        "query",
    }
)


@dataclass
class RouteDecision:
    route: str
    reason: str = ""
    model: str | None = None
    stripped_text: str | None = None
    synth_reply: str | None = None
    question: str | None = None
    payload: str | None = None
    response: str | None = None
    similarity: float | None = None
    source_model: str | None = None
    original_query: str | None = None
    have_groq: bool | None = None
    have_or: bool | None = None
    query: str | None = None
    extras: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> RouteDecision:
        if not isinstance(d, dict):
            raise TypeError(
                f"RouteDecision.from_dict expects dict, got {type(d).__name__}"
            )
        if "route" not in d:
            raise ValueError("RouteDecision requires a 'route' key")
        known = {k: d[k] for k in _KNOWN_KEYS if k in d}
        extras = {k: v for k, v in d.items() if k not in _KNOWN_KEYS}
        return cls(extras=extras, **known)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"route": self.route, "reason": self.reason}
        for k in _KNOWN_KEYS:
            if k in ("route", "reason"):
                continue
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        out.update(self.extras)
        return out


def route(history, user_text, image_path=None) -> dict:
    """Pick a route for the user turn.

    Delegates to master_ai.orchestrate(). Returns the same dict shape today;
    callers should treat 'route' and 'reason' as always present and read
    everything else defensively.
    """
    decision = master_ai.orchestrate(history, user_text, image_path=image_path)
    if not isinstance(decision, dict) or "route" not in decision:
        raise RuntimeError(
            f"master_ai.orchestrate returned malformed decision: {decision!r}"
        )
    return decision


def detect(text, has_image=False):
    """Pass-through to master_ai.detect_route(text, has_image)."""
    return master_ai.detect_route(text, has_image=has_image)
