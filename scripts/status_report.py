"""
Shared status field builder for Sensei.

Provides a single source of truth for computing common session status fields
(Session ID, Title, Model, Provider, Created, Last Activity, Tokens, Agent Running)
so that CLI, TUI, and any future interfaces render consistent data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class StatusFields:
    """Structured, display-ready status fields."""
    session_id: str
    title: str
    model: str
    provider: str
    created: str
    last_activity: str
    tokens: str
    agent_running: bool
    # Raw values for renderers that need them
    created_dt: Optional[dt.datetime] = None
    last_activity_dt: Optional[dt.datetime] = None
    tokens_raw: int = 0


def _parse_ts(value: Any, fallback: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    """Parse a timestamp from various formats, returning timezone-aware UTC datetime."""
    if value is None:
        return fallback
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        except (ValueError, OSError):
            return fallback
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                parsed = dt.datetime.strptime(value, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return parsed.astimezone(dt.timezone.utc)
            except ValueError:
                continue
    return fallback


def _fmt_ts(dt_obj: Optional[dt.datetime]) -> str:
    """Format datetime as 'YYYY-MM-DD HH:MM' in local time, or placeholder."""
    if dt_obj is None:
        return "—"
    try:
        local = dt_obj.astimezone()
        return local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _fmt_tokens(count: int) -> str:
    """Format token count with thousands separators."""
    return f"{count:,}"


def build_status_fields(
    session_id: str,
    agent: Optional[Any] = None,
    session_meta: Optional[dict[str, Any]] = None,
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    created_fallback: Optional[dt.datetime] = None,
    agent_running: bool = False,
) -> StatusFields:
    """
    Compute all common status fields from session metadata and agent state.

    Args:
        session_id: Unique session identifier.
        agent: Optional agent instance to extract model/provider/token info.
        session_meta: Optional dict from SessionDB (keys: title, started_at, updated_at, etc.).
        model: Explicit model override (e.g., from CLI config).
        provider: Explicit provider override.
        created_fallback: Fallback creation time (e.g., CLI session_start).
        agent_running: Whether the agent loop is currently active.

    Returns:
        StatusFields with all display-ready values populated.
    """
    session_meta = session_meta or {}

    # Title
    title = (session_meta.get("title") or "").strip()

    # Created timestamp: prefer started_at, fall back to created_fallback, then now()
    created_dt = _parse_ts(session_meta.get("started_at"), created_fallback)
    if created_dt is None:
        created_dt = dt.datetime.now(dt.timezone.utc)

    # Last activity: scan common fields in priority order
    last_activity_dt = None
    for field in ("updated_at", "last_updated_at", "last_activity_at", "started_at"):
        candidate = _parse_ts(session_meta.get(field))
        if candidate is not None:
            last_activity_dt = candidate
            break
    if last_activity_dt is None:
        last_activity_dt = created_dt

    # Model / Provider: agent attributes > explicit args > session_meta > defaults
    if agent is not None:
        model = model or getattr(agent, "model", None) or getattr(agent, "model_name", None)
        provider = provider or getattr(agent, "provider", None) or getattr(agent, "provider_name", None)
    model = (model or session_meta.get("model") or "").strip() or "(unknown)"
    provider = (provider or session_meta.get("provider") or "").strip() or "unknown"

    # Tokens: agent.session_total_tokens > session_meta.tokens > 0
    tokens_raw = 0
    if agent is not None:
        tokens_raw = getattr(agent, "session_total_tokens", 0) or 0
    if tokens_raw == 0:
        tokens_raw = session_meta.get("tokens", 0) or 0

    return StatusFields(
        session_id=session_id,
        title=title,
        model=model,
        provider=provider,
        created=_fmt_ts(created_dt),
        last_activity=_fmt_ts(last_activity_dt),
        tokens=_fmt_tokens(tokens_raw),
        agent_running=bool(agent_running),
        created_dt=created_dt,
        last_activity_dt=last_activity_dt,
        tokens_raw=tokens_raw,
    )


def status_lines(fields: StatusFields, *, prefix: str = "") -> list[str]:
    """
    Render the common status fields as "Label: value" lines.

    Args:
        fields: StatusFields from build_status_fields().
        prefix: Optional prefix for each line (e.g., "  " for indentation).

    Returns:
        List of formatted lines ready for printing.
    """
    lines = [
        f"{prefix}Session ID: {fields.session_id}",
    ]
    if fields.title:
        lines.append(f"{prefix}Title: {fields.title}")
    lines.extend([
        f"{prefix}Model: {fields.model}",
        f"{prefix}Provider: {fields.provider}",
        f"{prefix}Created: {fields.created}",
        f"{prefix}Last Activity: {fields.last_activity}",
        f"{prefix}Tokens: {fields.tokens}",
        f"{prefix}Agent Running: {'Yes' if fields.agent_running else 'No'}",
    ])
    return lines
