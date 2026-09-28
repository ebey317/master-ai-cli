"""
Pytest fixtures for approval gate isolation.

Ensures each test gets a clean approval state and no leaked callbacks.
"""

from __future__ import annotations

import pytest

from scripts.approval_gate import clear_all_grants, set_explicit_callback


@pytest.fixture(autouse=True)
def _isolate_approval_state() -> None:
    """Reset approval stores and explicit callback after every test."""
    yield
    clear_all_grants()
    set_explicit_callback(None)
