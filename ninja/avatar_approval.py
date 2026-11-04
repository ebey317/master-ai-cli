#!/usr/bin/env python3
"""Avatar-driven approval surface — card spawns FROM avatar.

The approval card is anchored beside the avatar and lives in the same
window family (ThootieOverlay expands horizontally: avatar left, card right).
No second approval window is opened — the card visually grows out of the
avatar via spawn animation (smoothstep width growth) while the avatar
continues perpetual gentle float. Theme-palette only, crisp PNG, clean edges.
"""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=DeprecationWarning)

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
    timeout: int | None = DEFAULT_TIMEOUT,
) -> str:
    """Show approval card spawned FROM avatar, same window family.

    - Anchored beside avatar (avatar left, card right in same overlay)
    - Spawn animation: window grows from IDLE_SIZE to ACTIVE_WIDTH
    - Perpetual float continues underneath — never stationary
    - Fail-closed: any error returns 'deny'
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
        # present_approval triggers _set_active(True) which starts spawn anim
        # Tahuti's --ask is an interactive human gate; wait for a decision.
        verdict = overlay.present_approval(
            timeout=None if agent == "tahuti" else timeout
        )
        overlay.park_idle()
        return verdict
    except Exception:
        return "deny"


def show_avatar_during_run(agent: str, card: dict | str) -> None:
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
    print("construct ok (display available) — spawn-from-avatar, same family")
    overlay.set_card_text(
        "WHO: Tahuti agent\nWHAT: self-test construct\nWHERE: local\nWHEN: now\nHOW: import + build widgets"
    )
    print("selftest PASS")
    return 0


def _read_card(argv: list[str]) -> str:
    if len(argv) > 1:
        path = Path(argv[1])
        if path.is_file():
            return path.read_text()
    return sys.stdin.read()


def _ask_cli(argv: list[str]) -> int:
    if "DISPLAY" not in os.environ:
        os.environ["DISPLAY"] = ":0"
    card = _read_card(argv)
    print(ask_avatar_approval(card, agent="tahuti"))
    return 0


def _pulse_cli(args: list[str]) -> int:
    """--pulse [N]: L+R summon — floating avatar with live transcript."""
    import json as _json
    import time as _time

    from gi.repository import GLib

    secs = 12
    for a in args:
        try:
            secs = max(4, min(60, int(a)))
            break
        except ValueError:
            continue
    if "DISPLAY" not in os.environ:
        os.environ["DISPLAY"] = ":0"
    cap_path = Path.home() / ".config" / "ai-controller" / "tahuti_caption.json"
    try:
        show_avatar_during_run("tahuti", "Listening — hold L+R, speak, release.")
        from ninja.state_machine import get_overlay

        overlay = get_overlay(agent="tahuti")
    except Exception:
        return 0
    if overlay is None:
        return 0
    t0 = _time.time()

    _refresh_done_at: list = []
    _refresh_last: list = []
    # Sliding-window lifetime, not a fixed one-shot deadline (fixed 2026-10-31):
    # a hard GLib.timeout_add_seconds(secs, loop.quit) used to kill this whole
    # process at t0+secs no matter what, so ptt_pynput.py's on_press handler
    # had to SIGKILL and respawn a brand-new pulse for any second genuinely
    # separate press within that window -- wiping the first take's "Done"
    # state off-screen mid-flight and leaving a brief window-less gap during
    # the kill/respawn. Instead, the window now stays open as long as the
    # caption file keeps getting fresh writes (i.e. as long as real takes
    # keep happening), closing `secs` after the LAST real activity rather
    # than `secs` after the window was first opened. ptt_pynput.py's
    # on_press no longer needs to kill this process for a second real press
    # within the window -- it can just keep writing captions and let this
    # loop's own activity check extend naturally.
    # EVENT-DRIVEN LIFETIME (2026-11-03, operator spec): the window is alive
    # exactly as long as something real is happening — trigger held, work
    # running, or the model's voice still playing — and goes away when that
    # stops. No fixed clock decides when a reply is "long enough".
    #   listening / sending / heard / working -> stay
    #   done                                  -> stay while TTS plays (policy below)
    #   idle                                  -> leave now (ptt: nothing to do)
    _MAX_WORK_S = 900.0  # safety net only: a runner that died mid-work
    _done: dict[str, Any] = {
        "at": 0.0,
        "cap_time": 0.0,
        "spoke": False,
        "last_speech": 0.0,
    }

    def _tts_playing() -> bool:
        """True while the bridge's TTS player (hermes_tts_play.sh / the mpv
        fallback tagged AI_TTS_BARGE) is alive."""
        import subprocess as _sp

        # Match by exact process NAME, not `pgrep -f` (which also matches any
        # shell whose command line merely mentions the pattern): the wrapper
        # script, or an mpv carrying the AI_TTS_BARGE title.
        out = []
        try:
            out += _sp.run(
                ["pgrep", "-x", "hermes_tts_play"],
                stdout=_sp.PIPE,
                stderr=_sp.DEVNULL,
                text=True,
            ).stdout.split()
            for pid in _sp.run(
                ["pgrep", "-x", "mpv"], stdout=_sp.PIPE, stderr=_sp.DEVNULL, text=True
            ).stdout.split():
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        if b"AI_TTS_BARGE" in f.read():
                            out.append(pid)
                except Exception:
                    continue
        except Exception:
            return False
        # pgrep -f also matches ZOMBIES: the voice bridge never reaps its
        # Popen'd players, so finished ones linger as <defunct> forever and
        # made "speaking" permanently true (found 2026-11-03: window stayed
        # up 40s after the voice stopped). Only a live (non-Z) process counts.
        for pid in out:
            try:
                with open(f"/proc/{pid}/stat") as f:
                    if f.read().rsplit(")", 1)[1].split()[0] != "Z":
                        return True
            except Exception:
                continue
        return False

    def _should_stay_open(
        state: str, speaking: bool, since_done: float, ever_spoke: bool
    ) -> bool:
        """Decide whether the window stays up on this 500ms tick."""
        if state in ("listening", "sending", "heard", "working"):
            return True
        if state == "idle":
            return False
        # state == "done": the model finished; the voice may still be talking.
        # Policy (operator spec 2026-11-03): the model's voice decides, not a
        # clock. Mid-sentence is never cut. TTS generation takes a few seconds
        # after "done", so if it hasn't started yet wait up to 10s before
        # concluding it never will (muted / barge-in suppressed / bridge down).
        # Once it has played and stopped, leave after a 1s gap (covers
        # back-to-back players) — no lingering timer.
        if speaking:
            return True
        if not ever_spoke:
            return since_done < 10.0
        return _done_quiet_for() < 1.0

    def _done_quiet_for() -> float:
        """Seconds since TTS was last seen playing for the current reply."""
        return _time.time() - _done["last_speech"]

    loop = GLib.MainLoop()
    _state: dict[str, Any] = {"name": "listening", "t": t0}

    def _refresh() -> bool:
        # Change-guarded (2026-10-31): re-setting identical card text every
        # 500ms caused relayout flicker ("scrolling credits" glitch).
        now = _time.time()
        try:
            data = _json.loads(cap_path.read_text())
            cap_time = float(data.get("time", 0))
            # A caption written before this window opened belongs to an
            # earlier take — ignore it (replaces the old 60s staleness clock).
            if cap_time < t0 - 5:
                raise ValueError("caption from an earlier take")
            text, state = (data.get("text") or ""), (data.get("state") or "")
            _state["name"], _state["t"] = state or "listening", cap_time
            if state == "idle":
                new = ""
            elif state in ("sending",) or (state == "listening") or not text:
                new = "Listening — hold L+R, speak, release."
            elif state == "working":
                new = f"Working on:\n{text}"
            elif state == "done":
                new = f"Done:\n{text[:1200]}"
                if _done["cap_time"] != cap_time:  # a NEW done: reset its clock
                    _done.update(at=now, cap_time=cap_time, spoke=False)
            else:
                new = f"Heard — sending to agent:\n{text}"
            if new and (not _refresh_last or _refresh_last[0] != new):
                _refresh_last.clear()
                _refresh_last.append(new)
                overlay.set_card_text(new)
        except Exception:
            pass
        name = _state["name"]
        speaking = _tts_playing() if name == "done" else False
        if speaking:
            _done["spoke"] = True
            _done["last_speech"] = now
        since_done = now - _done["at"] if _done["at"] else 0.0
        stay = _should_stay_open(name, speaking, since_done, _done["spoke"])
        # Safety nets for a dead producer: working forever, or a trigger that
        # was "held" for over a minute with no event.
        if name == "working" and now - _state["t"] > _MAX_WORK_S:
            stay = False
        # "sending"/"heard" last only as long as speech-to-text takes; if the producer
        # died or bailed (too-short take) nothing else would ever close the window.
        if name in ("sending", "heard") and now - _state["t"] > 45:
            stay = False
        if name == "listening" and now - _state["t"] > max(secs, 60):
            stay = False
        if not stay:
            loop.quit()
            return False
        return True

    try:
        GLib.timeout_add(500, _refresh)
        loop.run()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if "--pulse" in sys.argv:
        raise SystemExit(_pulse_cli(sys.argv[sys.argv.index("--pulse") + 1 :]))
    if "--ask" in sys.argv:
        raise SystemExit(_ask_cli(sys.argv[sys.argv.index("--ask") + 1 :]))
    test_card = (
        "WHO: Tahuti agent\n"
        "WHAT: open calculator\n"
        "WHERE: desktop\n"
        "WHEN: now\n"
        "HOW: run_command\n"
        "DELEGATED TO: muse-spark-1.1"
    )
    print(ask_avatar_approval(test_card, agent="viking"))
