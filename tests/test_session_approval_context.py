"""Test session approval context helper for execute_code approval skipping."""

import pytest

from scripts.session_approval_context import is_local_session_own_turn

LOCAL_SOURCES = {"cli", "tui", "gui", "acp"}
NON_LOCAL_SOURCES = {"api", "rpc", "workflow"}


@pytest.mark.parametrize(
    "source,delegated,has_notifier,expected",
    [
        # Local session own turn: should skip
        ("cli", False, True, True),
        ("tui", False, True, True),
        ("gui", False, True, True),
        ("acp", False, True, True),
        # Local session but delegated task: should not skip
        ("cli", True, True, False),
        ("tui", True, True, False),
        ("gui", True, True, False),
        ("acp", True, True, False),
        # Local session but no notifier: should not skip
        ("cli", False, False, False),
        ("tui", False, False, False),
        ("gui", False, False, False),
        ("acp", False, False, False),
        # Non-local session: should not skip (regardless of other flags)
        ("api", False, True, False),
        ("api", True, True, False),
        ("api", False, False, False),
        ("rpc", False, True, False),
        ("workflow", True, False, False),
    ],
)
def test_is_local_session_own_turn(source, delegated, has_notifier, expected):
    assert is_local_session_own_turn(source, delegated, has_notifier) == expected
