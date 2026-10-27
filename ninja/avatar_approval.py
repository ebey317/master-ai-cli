#!/usr/bin/env python3
"""Avatar-driven approval surface for Tahuti.

Reuses the persistent Ninja overlay (ThootieOverlay) so the avatar stays
visible for the whole headless run.  The approval card is shown in the same
expanded overlay window with approve/deny buttons; no second approval window is
opened.

Usage from Tahuti:
    from ninja.avatar_approval import ask_avatar_approval
    verdict = ask_avatar_approval(card, agent="tahuti")

Returns one of: "once" | "session" | "always" | "deny".
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Make scripts.theme importable when this module is loaded stand-alone.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

from ninja.state_machine import (  # noqa: E402
    _GTK_AVAILABLE,
    ThootieState,
    get_overlay,
)

DEFAULT_TIMEOUT = 120


def _normalize_card(card: dict | str) -> str:
    if isinstance(card, dict):
        from ninja.card_box import render_card_text

        return render_card_text(card)
    return str(card)


def ask_avatar_approval(
    card: dict | str,
    agent: str = "ninja",
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Show the avatar approval surface and return the verdict.

    ``agent`` is one of "ninja" / "tahuti" / "viking".  ``card`` may be the
    queue-mapped dict used by card_box or the plain-English string Tahuti
    currently builds.  Fail-closed: any error or missing display returns
    "deny".

    The persistent avatar overlay is reused; no second approval window is
    created.  After the verdict the overlay parks back to idle.
    """
    if not _GTK_AVAILABLE:
        return "deny"
    try:
        overlay = get_overlay(agent=agent)
        if overlay is None:
            return "deny"
        overlay.set_agent(agent)
        overlay.set_card_text(_normalize_card(card))
        overlay.machine.set_state(
            ThootieState.APPROVAL_INCOMING,
            overlay.machine.payload if overlay.machine.payload else None,
        )
        if not overlay._win.get_visible():
            overlay.show()
        verdict = overlay.present_approval(timeout=timeout)
        overlay.park_idle()
        return verdict
    except Exception:
        return "deny"


def show_avatar_during_run(agent: str, card: dict | str) -> None:
    """Public alias for callers that want the run-time avatar card."""
    from ninja.state_machine import show_avatar as _show_avatar

    _show_avatar(agent, card)


def _selftest() -> int:
    print("import ok")
    if not _GTK_AVAILABLE:
        print("SKIP: GTK not available")
        return 0
    try:
        overlay = get_overlay(agent="ninja")
    except Exception as exc:
        print(f"SKIP: overlay unavailable ({exc})")
        return 0
    if overlay is None or overlay._win is None:
        print("SKIP: no GTK display available; headless-safe construct ok")
        return 0
    print("construct ok (display available)")
    overlay.set_card_text(
        "WHO: Tahuti agent\nWHAT: self-test construct\nWHERE: local\nWHEN: now\nHOW: import + build widgets"
    )
    print("selftest PASS")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    test_card = (
        "WHO: Tahuti agent\n"
        "WHAT: open calculator\n"
        "WHERE: desktop\n"
        "WHEN: now\n"
        "HOW: run_command\n"
        "DELEGATED TO: muse-spark-1.1"
    )
    print(ask_avatar_approval(test_card, agent="viking"))
