"""
Tool-prefix pinning utilities for Sensei.

Preserves the wire-order tool array across surface/mode switches so that the
KV-cache prefix (which serializes tools[] before the system prompt) remains
valid. Tools that the new surface did not load are annotated in a one-shot
system note so the model does not plan around unavailable capabilities.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _agent_tool_names(agent: Any) -> list[str]:
    """Return the names in ``agent.tools`` in wire order ([] if unreadable)."""
    names: list[str] = []
    try:
        for entry in getattr(agent, "tools", None) or []:
            if isinstance(entry, dict):
                name = (entry.get("function") or {}).get("name", "")
            else:
                # Some backends expose objects with .function.name
                name = getattr(getattr(entry, "function", None), "name", "")
            if name:
                names.append(name)
    except Exception:
        return []
    return names


def _merge_preserving_prefix(pinned: list[str], newly_built: list[str]) -> list[str]:
    """
    Return a tool list that keeps the pinned prefix and appends any new tools
    from the freshly-built list that aren't already present.

    This mirrors the upstream ``_merge_preserving_prefix`` semantics: the
    frozen prefix wins for ordering and presence; the new surface's additions
    are appended so a tui->desktop switch still converges.
    """
    seen = set(pinned)
    merged = list(pinned)
    for name in newly_built:
        if name not in seen:
            merged.append(name)
            seen.add(name)
    return merged


def pin_tool_prefix(agent: Any, session_row: dict | None) -> bool:
    """
    Restore the saved tool prefix from ``session_row`` onto ``agent``.

    Returns True if a prefix was restored, False otherwise.
    """
    if not session_row:
        return False
    saved = session_row.get("tool_names")
    if not saved:
        return False

    try:
        pinned = json.loads(saved)
    except Exception:
        logger.debug("tool prefix restore skipped: bad JSON", exc_info=True)
        return False

    # Capture what the current surface would have built *before* we overwrite.
    built_for_this_surface = _agent_tool_names(agent)

    # Restore the pinned prefix (this mutates agent.tools in-place in the
    # upstream; here we expose the merged list for the caller to assign).
    merged = _merge_preserving_prefix(pinned, built_for_this_surface)

    # The caller is responsible for assigning back to agent.tools if needed.
    # We return the merged list and the inert names for annotation.
    agent._pinned_tool_names = merged
    agent._inert_tool_names = [
        name for name in pinned if name not in set(built_for_this_surface)
    ]
    return True


def annotate_inert_tools(agent: Any) -> None:
    """
    Append a one-shot note naming the pinned-but-unavailable tools.

    Rides on ``agent._surface_switch_note`` so it lands behind the cached
    prefix like the rest of the correction.
    """
    inert = getattr(agent, "_inert_tool_names", [])
    if not inert:
        return

    note = getattr(agent, "_surface_switch_note", "") or ""
    agent._surface_switch_note = note + (
        "\n[System: These tools stay listed for this conversation (dropping them "
        "would discard the cached request prefix) but were not loaded for this "
        f"interface — expect a call to one of them to fail: {', '.join(inert)}.]"
    )


def persist_tool_names(agent: Any) -> list[str]:
    """
    Return the current wire-order tool names for persistence.

    Call this at the *next* real rebuild boundary (i.e. not on the announcing
    turn) so the converged toolset becomes the new frozen prefix.
    """
    return _agent_tool_names(agent)
