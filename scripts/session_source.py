"""Session source resolution for Sensei.

Portable design from upstream: a one-shot child spawned from an interactive UI
session inherits the parent's SESSION_SOURCE env var, but should be labelled
with its own platform (cli) rather than the UI transport (tui/desktop) so it
does not appear in UI session pickers as a resumable conversation. Automation
sources (kanban, tool, cron, a2a, ...) are intentionally inherited.
"""

import os
from typing import Optional

# Sources that label the human conversation an interactive UI transport hosts.
# A finite one-shot child spawned from such a session inherits SESSION_SOURCE
# but is NOT that conversation: labelling it tui/desktop lists it in the TUI
# pickers as a resumable chat and lets `sensei -c` in the TUI continue it.
_UI_TRANSPORT_SOURCES = frozenset({"tui", "desktop"})

# Sources that represent automation/orchestration and SHOULD be inherited by
# child one-shot runs (e.g., kanban dispatch relies on this).
_AUTOMATION_SOURCES = frozenset({"kanban", "tool", "cron", "a2a"})

# Environment variable markers
SESSION_SOURCE_ENV = "SENSEI_SESSION_SOURCE"
SINGLE_QUERY_SESSION_ENV = "SENSEI_SINGLE_QUERY_SESSION"


def resolve_session_source(platform: Optional[str] = None) -> str:
    """Resolve the effective session source for the current process.

    Args:
        platform: The platform label of the current entry point (e.g., "cli", "tui").
                  If None, defaults to "cli".

    Returns:
        The resolved source string.
    """
    # Read from environment (bridged from parent session if any)
    inherited = os.environ.get(SESSION_SOURCE_ENV, "").strip()
    is_single_query = os.environ.get(SINGLE_QUERY_SESSION_ENV, "") == "1"

    # If we're a finite one-shot child and the inherited source is a UI transport,
    # drop it and fall back to our own platform.
    if is_single_query and inherited in _UI_TRANSPORT_SOURCES:
        inherited = ""

    # Automation sources are inherited on purpose.
    # (No special handling needed; they pass through.)

    return inherited or platform or "cli"


def mark_single_query_session() -> None:
    """Mark the current process as a finite one-shot run.

    Call this early in one-shot entry points (e.g., `sensei -q`, `sensei -z`).
    """
    os.environ[SINGLE_QUERY_SESSION_ENV] = "1"
    # Non-interactive by definition — an approval prompt would hang forever.
    os.environ["SENSEI_YOLO_MODE"] = "1"
    os.environ["SENSEI_ACCEPT_HOOKS"] = "1"


def set_session_source(source: str) -> None:
    """Explicitly set the session source for the current process.

    Used by entry points that know their source (e.g., TUI sets "tui",
    CLI sets "cli", kanban dispatcher sets "kanban").
    """
    os.environ[SESSION_SOURCE_ENV] = source
