"""Rate-limit reset surface for Sensei.

Portable design idea (from hermes-agent 429 error-card work): when an HTTP 429
or quota error carries provider timing data (Retry-After header value,
``resets_at`` epoch seconds, or ``retry_after`` seconds), format one plain
line — "Limit resets at 14:05 (in 1h 00m)." — that any Sensei text surface
(CLI output, hook events, notification channels) can render verbatim.

This module is pure formatting/parsing; wiring into the agent loop's error
path happens via the hook bus (see LIMIT_RESET_EVENT / LEVEL_ERROR payloads).
No scheduled/automatic retry is performed — informational only.
"""

from __future__ import annotations

import time
from typing import Any


def extract_resets_at(error: Any) -> float | None:
    """Best-effort extraction of the moment the usage limit lifts (epoch s).

    Accepts:
      - ``resets_at`` (epoch seconds) — preferred, absolute
      - ``retry_after`` (seconds) — relative
      - ``Retry-After`` header string (seconds or HTTP-date)
      - fallback: error str containing "resets_at=<epoch>"
    """
    if isinstance(error, dict):
        for key in ("resets_at", "failure_resets_at", "reset_at"):
            val = error.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                return float(val)
        for key in ("retry_after", "retry_after_s"):
            val = error.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                return time.time() + float(val)
        headers = error.get("headers") or {}
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra is not None:
            return _parse_retry_after(ra)
        message = error.get("error") or error.get("message") or ""
    else:
        message = str(error)
    return _parse_message(message)


def _parse_retry_after(raw: str) -> float | None:
    raw = str(raw).strip()
    if raw.isdigit():
        return time.time() + int(raw)
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(raw).timestamp()
    except Exception:
        return None


def _parse_message(message: str) -> float | None:
    if "resets_at=" not in message:
        return None
    try:
        return float(message.split("resets_at=", 1)[1].split()[0])
    except Exception:
        return None


def format_human_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def format_limit_reset(resets_at: float | None, now: float | None = None) -> str | None:
    """One plain line for text surfaces, or None if unknown/already passed."""
    if resets_at is None:
        return None
    now = now if now is not None else time.time()
    remaining = resets_at - now
    if remaining <= 0:
        return None  # already passed — surface nothing, Retry is plausible now
    hhmm = time.strftime("%H:%M", time.localtime(resets_at))
    return f"Limit resets at {hhmm} (in {format_human_delta(remaining)})."


def build_hook_payload(error: Any) -> dict[str, Any]:
    """Payload for hooks.emit(..., LIMIT_RESET_EVENT) so any consumer can render it."""
    resets_at = extract_resets_at(error)
    return {
        "resets_at": resets_at,
        "message": format_limit_reset(resets_at),
        "informational_only": True,
    }
