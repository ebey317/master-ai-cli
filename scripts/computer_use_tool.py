"""
Computer-use tool for Sensei — approval integration example.

This tool demonstrates how to use the shared approval gate instead of
rolling a private approval system.  The pattern key format is:

    cua:<action>:<mode>

where <mode> is "foreground" (visible window) or "background" (headless).
A grant for "cua:click:background" does NOT cover "cua:click:foreground".
"""

from __future__ import annotations

from scripts.approval_gate import Verdict, request_approval, set_explicit_callback

# ──────────────────────────────────────────────────────────────────────
# Tool implementation
# ──────────────────────────────────────────────────────────────────────


def _build_pattern_key(action: str, background: bool) -> str:
    mode = "background" if background else "foreground"
    return f"cua:{action}:{mode}"


def _request_approval(action: str, background: bool, description: str) -> Verdict:
    """
    Ask the shared gate for permission to perform a computer-use action.

    Uses fail_closed_when_no_human=True so that cron, API server, headless
    runs, etc. get a hard DENY unless an explicit callback is installed.
    """
    pattern_key = _build_pattern_key(action, background)
    return request_approval(
        pattern_key,
        description,
        fail_closed_when_no_human=True,
        action=action,
        background=background,
    )


def click(x: int, y: int, *, background: bool = False) -> str:
    """Click at (x, y)."""
    verdict = _request_approval(
        "click",
        background,
        f"Click at ({x}, {y}) {'(background)' if background else '(foreground)'}",
    )
    if verdict == Verdict.DENY:
        return "BLOCKED: User denied computer_use click"
    if verdict == Verdict.TIMEOUT:
        return "BLOCKED: Action timed out waiting for approval"
    # verdict in (ONCE, SESSION, ALWAYS) → proceed
    return f"clicked at ({x}, {y})"


def type_text(text: str, *, background: bool = False) -> str:
    """Type text."""
    verdict = _request_approval(
        "type",
        background,
        f"Type text: {text!r} {'(background)' if background else '(foreground)'}",
    )
    if verdict == Verdict.DENY:
        return "BLOCKED: User denied computer_use type"
    if verdict == Verdict.TIMEOUT:
        return "BLOCKED: Action timed out waiting for approval"
    return f"typed {len(text)} characters"


def screenshot(*, background: bool = False) -> str:
    """Take a screenshot."""
    verdict = _request_approval(
        "screenshot",
        background,
        f"Take screenshot {'(background)' if background else '(foreground)'}",
    )
    if verdict == Verdict.DENY:
        return "BLOCKED: User denied computer_use screenshot"
    if verdict == Verdict.TIMEOUT:
        return "BLOCKED: Action timed out waiting for approval"
    return "screenshot captured"


# ──────────────────────────────────────────────────────────────────────
# Optional explicit callback hook (kept for parity with upstream)
# ──────────────────────────────────────────────────────────────────────


def set_approval_callback(cb: callable | None) -> None:
    """
    Back-compat shim: install an explicit callback for computer_use.

    The callback must accept (action, args, summary) and return a Verdict
    or the legacy string variants.  This wraps the shared gate's
    `set_explicit_callback`.
    """
    if cb is None:
        set_explicit_callback(None)
        return

    def wrapper(pattern_key: str, description: str, **kw) -> Verdict:
        # Extract action from pattern_key "cua:<action>:<mode>"
        parts = pattern_key.split(":")
        action = parts[1] if len(parts) >= 3 else "unknown"
        # Call legacy callback signature
        result = cb(action, kw, description)
        if isinstance(result, Verdict):
            return result
        # Map legacy strings
        mapping = {
            "approve_once": Verdict.ONCE,
            "approve_session": Verdict.SESSION,
            "always_approve": Verdict.ALWAYS,
            "deny": Verdict.DENY,
            "timeout": Verdict.TIMEOUT,
        }
        return mapping.get(str(result).lower(), Verdict.DENY)

    set_explicit_callback(wrapper)
