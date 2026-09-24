"""
Availability gate for connector/plugin tools.

The upstream bug: a third "free-tier guest" leg was imported from another
branch and raised ImportError, silently disabling all connectors. The portable
design is a strict two-leg AND: feature flag enabled AND session entitlement
present. For Sensei that maps to config flag + local auth state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class AuthState:
    connectors_enabled: bool = False
    user_authenticated: bool = False


AvailabilityCheck = Callable[[], AuthState]


def connectors_available(check: AvailabilityCheck) -> bool:
    """Return True only when both required legs are satisfied.

    Fail-closed: any exception returns False so the agent never leaks
    unentitled connector calls.
    """
    try:
        state = check()
    except Exception:
        return False
    return state.connectors_enabled and state.user_authenticated
