#!/usr/bin/env python3
"""Read-only poll of the approval queue for Thootie.

Reads the canonical JSONL store directly
(``~/scripts/.pending_actions.jsonl``) instead of importing
approval_queue — no handler loading, no branch sensitivity, no writes,
ever.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_QUEUE = Path.home() / "scripts" / ".pending_actions.jsonl"

DETERMINISTIC_MODEL_LINE = "deterministic code — no model, $0"


def pending_first(path: Path | None = None) -> dict | None:
    """Return the first PENDING entry in file order, or None.

    Missing file, blank lines, and corrupt lines all yield no entry
    rather than an exception — the avatar must never crash on queue
    state it cannot parse.
    """
    queue = path or DEFAULT_QUEUE
    try:
        text = queue.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("status") == "PENDING":
            return entry
    return None


def entry_to_card(entry: dict) -> dict:
    """Map a queue entry onto the spawned-box card fields."""
    payload = entry.get("payload") or {}
    return {
        "who": entry.get("who", ""),
        "what": entry.get("what", ""),
        "where": entry.get("where", ""),
        "when": entry.get("when", ""),
        "why": entry.get("why", ""),
        "how": entry.get("how", ""),
        "question": payload.get("question") or entry.get("what", ""),
        "executor": f"Master AI — handler {entry.get('type', '?')}",
        "model": payload.get("model") or DETERMINISTIC_MODEL_LINE,
    }
