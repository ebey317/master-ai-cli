"""
Tests for the shared approval gate (ported from hermes-agent
test_computer_use_approval_isolation.py).
"""

from __future__ import annotations

from scripts.approval_gate import (
    Verdict,
    clear_all_grants,
    clear_session_grants,
    is_approved,
    list_grants,
    request_approval,
    set_explicit_callback,
    set_thread_callback,
)
from scripts.computer_use_tool import (
    click,
    screenshot,
    set_approval_callback,
    type_text,
)


def test_no_callback_refuses_unless_yolo() -> None:
    """
    With no callback installed and fail_closed=True (default),
    destructive actions are REFUSED and never reach the backend.
    """
    clear_all_grants()
    set_explicit_callback(None)
    set_thread_callback(None)

    # Computer-use click should be denied
    result = click(100, 200, background=False)
    assert result == "BLOCKED: User denied computer_use click"

    # No backend call would have been made (simulated by not executing the action)
    # In real code, the tool would return early before calling the backend.


def test_yolo_mode_allows() -> None:
    """
    With fail_closed_when_no_human=False (simulating --yolo / approvals.mode: off),
    actions are allowed without a callback.
    """
    clear_all_grants()
    set_explicit_callback(None)
    set_thread_callback(None)

    # Directly call request_approval with fail_closed=False
    verdict = request_approval(
        "cua:click:foreground",
        "Click at (100, 200)",
        fail_closed_when_no_human=False,
    )
    assert verdict == Verdict.ONCE


def test_explicit_callback_grants_session() -> None:
    """
    An explicit callback can grant SESSION approval, which persists
    for subsequent calls in the same session.
    """
    clear_all_grants()

    def my_callback(pattern_key: str, description: str, **kw) -> Verdict:
        return Verdict.SESSION

    set_explicit_callback(my_callback)

    # First call -> prompts callback -> SESSION grant stored
    result1 = click(100, 200, background=False)
    assert "clicked" in result1

    # Second call -> served from session store, no callback invoked
    result2 = click(300, 400, background=False)
    assert "clicked" in result2

    # Verify grant is in session store
    session_grants, always_grants = list_grants()
    assert "cua:click:foreground" in session_grants
    assert "cua:click:foreground" not in always_grants

    # Clear session -> next call prompts again
    clear_session_grants()
    call_count = 0

    def counting_callback(pattern_key: str, description: str, **kw) -> Verdict:
        nonlocal call_count
        call_count += 1
        return Verdict.ONCE

    set_explicit_callback(counting_callback)
    click(500, 600, background=False)
    assert call_count == 1


def test_always_grant_lands_in_shared_store() -> None:
    """
    An ALWAYS verdict writes to the permanent allowlist, visible to
    is_approved() and surviving session clears.
    """
    clear_all_grants()

    def always_callback(pattern_key: str, description: str, **kw) -> Verdict:
        return Verdict.ALWAYS

    set_explicit_callback(always_callback)

    # First call -> ALWAYS grant stored
    result = click(100, 200, background=False)
    assert "clicked" in result

    # is_approved sees the key
    assert is_approved("cua:click:foreground") is True

    # Clear session does NOT clear permanent grants
    clear_session_grants()
    assert is_approved("cua:click:foreground") is True

    # New call served from permanent store, no callback invoked
    call_count = 0

    def counting_callback(pattern_key: str, description: str, **kw) -> Verdict:
        nonlocal call_count
        call_count += 1
        return Verdict.DENY

    set_explicit_callback(counting_callback)
    result = click(300, 400, background=False)
    assert "clicked" in result
    assert call_count == 0  # callback not called


def test_background_grant_does_not_cover_foreground() -> None:
    """
    Pattern keys distinguish background vs foreground.
    A grant for 'cua:click:background' does NOT cover 'cua:click:foreground'.
    """
    clear_all_grants()

    def bg_callback(pattern_key: str, description: str, **kw) -> Verdict:
        return Verdict.ALWAYS

    set_explicit_callback(bg_callback)

    # Grant background click
    click(100, 200, background=True)
    assert is_approved("cua:click:background") is True
    assert is_approved("cua:click:foreground") is False

    # Foreground click still prompts
    call_count = 0

    def fg_callback(pattern_key: str, description: str, **kw) -> Verdict:
        nonlocal call_count
        call_count += 1
        return Verdict.ONCE

    set_explicit_callback(fg_callback)
    click(300, 400, background=False)
    assert call_count == 1


def test_legacy_callback_adapter() -> None:
    """
    The legacy `set_approval_callback` adapter correctly maps the old
    (action, args, summary) signature and legacy verdict strings.
    """
    clear_all_grants()

    def legacy_cb(action: str, args: dict, summary: str) -> str:
        return "approve_session"

    set_approval_callback(legacy_cb)

    result = click(100, 200, background=False)
    assert "clicked" in result

    session_grants, _ = list_grants()
    assert "cua:click:foreground" in session_grants


def test_deny_verdict_returns_blocked_message() -> None:
    """Explicit DENY verdict returns a BLOCKED error string."""
    clear_all_grants()

    def deny_callback(pattern_key: str, description: str, **kw) -> Verdict:
        return Verdict.DENY

    set_explicit_callback(deny_callback)

    result = click(100, 200, background=False)
    assert result == "BLOCKED: User denied computer_use click"

    result = type_text("hello", background=False)
    assert result == "BLOCKED: User denied computer_use type"

    result = screenshot(background=False)
    assert result == "BLOCKED: User denied computer_use screenshot"


def test_timeout_verdict_returns_blocked_message() -> None:
    """TIMEOUT verdict returns a BLOCKED error string."""
    clear_all_grants()

    def timeout_callback(pattern_key: str, description: str, **kw) -> Verdict:
        return Verdict.TIMEOUT

    set_explicit_callback(timeout_callback)

    result = click(100, 200, background=False)
    assert result == "BLOCKED: Action timed out waiting for approval"
