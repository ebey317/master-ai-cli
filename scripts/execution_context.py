"""Execution context detection for approval gating and feature availability.

Portable design from NousResearch/hermes-agent#04fcf915:
- Single-query (-q) and cron are TRULY unattended: no human, no async bridge.
  Clear all presence flags so approval gates resolve from config (block/allow).
- Platform contexts like api_server have an async approval bridge (approval.request
  -> HTTP callback). Keep `is_ask` true so dangerous commands park waiting for
  the bridge instead of instant-blocking.
- External cron workers receive a stripped env (no HERMES_INTERACTIVE, etc.);
  a dedicated HERMES_CRON_SESSION marker preserves cron tool eligibility.
"""

from __future__ import annotations

import os
from typing import Literal

from .config import env_var_enabled, is_truthy_value

ContextKind = Literal[
    "interactive", "single_query", "cron", "api_server", "webhook", "unknown"
]


def detect_context_kind() -> ContextKind:
    """Return the current execution context kind.

    Priority order matches upstream: explicit platform > cron marker > single-query > interactive.
    """
    platform = os.environ.get("SENSEI_SESSION_PLATFORM", "").lower()
    if platform == "api_server":
        return "api_server"
    if platform == "webhook":
        return "webhook"
    if is_truthy_value(os.environ.get("SENSEI_CRON_SESSION", "")):
        return "cron"
    if is_truthy_value(os.environ.get("SENSEI_SINGLE_QUERY", "")):
        return "single_query"
    if env_var_enabled("SENSEI_INTERACTIVE"):
        return "interactive"
    return "unknown"


def presence_flags() -> tuple[bool, bool, bool]:
    """Return (is_cli, is_gateway, is_ask) for the current context.

    - interactive:        (True,  True,  True)   -- human at terminal, gateway session
    - single_query:       (False, False, False)  -- no human, no bridge
    - cron:               (False, False, False)  -- no human, no bridge
    - api_server:         (False, True,  True)   -- gateway bridge, async approval via /v1/runs
    - webhook:            (False, True,  False)  -- platform session, no interactive ask
    - unknown:            (False, False, False)  -- safe default
    """
    kind = detect_context_kind()

    if kind == "interactive":
        return True, True, True
    if kind == "api_server":
        # Gateway bridge present; async approval bridge needs is_ask=True
        return False, True, True
    if kind == "webhook":
        # Platform session but no interactive ask surface
        return False, True, False
    # single_query, cron, unknown: truly unattended
    return False, False, False


def check_cronjob_requirements() -> bool:
    """Whether cron job toolset is available.

    True in interactive CLI, gateway/platform sessions, and cron runs.
    External cron workers have presence vars stripped; SENSEI_CRON_SESSION
    survives to keep `cron.allow_agent_scheduling` meaningful.
    """
    return (
        env_var_enabled("SENSEI_INTERACTIVE")
        or env_var_enabled("SENSEI_GATEWAY_SESSION")
        or env_var_enabled("SENSEI_EXEC_ASK")
        or is_truthy_value(os.environ.get("SENSEI_CRON_SESSION", ""))
    )


def is_truly_unattended() -> bool:
    """True for contexts where NO approval mechanism exists (human or async bridge)."""
    kind = detect_context_kind()
    return kind in ("single_query", "cron", "unknown")


def has_async_approval_bridge() -> bool:
    """True for contexts that can resolve approvals asynchronously (e.g., api_server)."""
    kind = detect_context_kind()
    return kind == "api_server"
