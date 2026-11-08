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
import time
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


# ---------------------------------------------------------------------------
# Avatar lifetime rules (module level so they can be tested without a display)
# ---------------------------------------------------------------------------
_SHELLS = {"bash", "sh", "dash", "zsh"}
_TTS_SCRIPTS = {"hermes_tts_play.sh", "hermes_tts_play"}
# Real audio players. Review round 3, finding 1: "any non-shell process whose
# argv mentions AI_TTS_BARGE" counted a `python -c`, a `grep` or an editor that
# merely quoted the tag as speech, which pins the card to its 180s cap. A
# process is a player only if it IS one of these (or the TTS script itself).
_TTS_PLAYERS = {"mpv", "ffplay", "paplay", "play", "aplay"}
_HERMES_VOICE_DIR = "/tmp/hermes_voice"

# The 500ms GLib tick listed /proc and read every cmdline on each pass (round 3,
# finding 2). A memo SHORTER than the poll never hits and is pure overhead
# (round 5, item 2), so the TTL sits just above 500ms: it saves most of the
# walks without letting a stale True stretch the 1s silence rule by more than
# half a tick.
_TTS_CACHE: dict[str, tuple[float, bool]] = {}
_TTS_CACHE_TTL_S = 0.6


def reset_tts_cache() -> None:
    """Drop tts_playing()'s memo so the next call really looks at the process table.

    Tests need this (a repeated call inside the 0.3 s TTL would otherwise re-check
    nothing), and so does anything that must see a change immediately.
    """
    _TTS_CACHE.clear()


def _is_player_name(name: str) -> bool:
    """Exact names only (round 5, item 4): a prefix match let any binary called
    `hermes_tts_play_something` claim to be speech."""
    return name in _TTS_PLAYERS or name in _TTS_SCRIPTS


def _first_non_option(argv: list[str]) -> str:
    """The first argv entry that is not a flag, by basename.

    A wrapper is `bash -x /path/hermes_tts_play.sh a.mp3` as often as
    `bash /path/hermes_tts_play.sh a.mp3`; checking argv[1] alone missed the
    first form entirely (round 5, item 4).
    """
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        return os.path.basename(arg)
    return ""


def _proc_is_live(pid: str, proc_root: str = "/proc") -> bool:
    """A non-zombie process. The voice bridge never reaps its Popen'd players, so
    finished ones linger as <defunct> and used to make "speaking" permanently true."""
    try:
        with open(f"{proc_root}/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except Exception:
        return False


def tts_playing(proc_root: str = "/proc") -> bool:
    """True while the bridge's TTS player is alive. Looks at REAL processes, not at
    `pgrep` name matches:

    - the player script itself (hermes_tts_play.sh, run directly or via a shell);
    - any non-shell process tagged AI_TTS_BARGE (the mpv the script starts, or the
      direct-mpv fallback).

    A shell whose command text merely MENTIONS those names (`bash -c "pgrep -f …"`)
    is not a player and is ignored, and neither is any other program that only
    carries the tag in its argv (review round 3, finding 1) -- the barge tag on
    its own is not evidence of speech.

    Cached for _TTS_CACHE_TTL_S per proc_root (finding 2): the caller runs this
    every 500ms and used to walk the whole process table each tick.
    """
    key = str(proc_root)
    hit = _TTS_CACHE.get(key)
    if hit is not None and time.monotonic() - hit[0] < _TTS_CACHE_TTL_S:
        return hit[1]
    value = _tts_playing_uncached(proc_root)
    _TTS_CACHE[key] = (time.monotonic(), value)
    return value


def _tts_playing_uncached(proc_root: str) -> bool:
    me = str(os.getpid())
    try:
        entries = [e for e in os.listdir(proc_root) if e.isdigit() and e != me]
    except OSError:
        return False
    for pid in entries:
        try:
            with open(f"{proc_root}/{pid}/cmdline", "rb") as f:
                argv = [
                    a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a
                ]
        except OSError:
            continue
        if not argv:
            continue
        argv0 = os.path.basename(argv[0])
        is_player = (
            argv0 in _TTS_SCRIPTS
            or (argv0 in _SHELLS and _first_non_option(argv) in _TTS_SCRIPTS)
            or (
                _is_player_name(argv0)
                and (
                    any("AI_TTS_BARGE" in a for a in argv[1:])
                    # Hermes' own ffplay/aplay carry no barge tag; they play from
                    # the voice scratch dir. Missing them cut long replies off
                    # mid-sentence (round 5, item 3).
                    or any(_HERMES_VOICE_DIR in a for a in argv[1:])
                )
            )
        )
        if is_player and _proc_is_live(pid, proc_root):
            return True
    return False


_DONE_WAIT_FOR_SPEECH_S = 10.0  # TTS generation takes a few seconds after "done"
_DONE_GAP_S = 1.0  # leave this long after the voice stops
_DONE_MAX_S = 180.0  # nothing legitimate talks longer than this


def should_stay_open(
    state: str, speaking: bool, since_done: float, ever_spoke: bool, quiet_for: float
) -> bool:
    """Whether the window stays up on this 500ms tick.

    listening / sending / heard / working -> stay
    idle                                  -> leave now (ptt: nothing to do)
    done -> the model's voice decides, not a clock (operator spec 2026-11-03):
        any 'done' older than 3 min -> leave FIRST, before the speaking check: the
                                       _DONE_MAX_S cap ends even a very long
                                       speech, on purpose, so a stuck or
                                       looping player cannot pin the window
                                       forever (so "mid-sentence is never cut"
                                       holds only up to that cap)
        speaking now                -> stay
        has not started yet         -> wait up to 10s, then conclude it never will
        spoke and has been quiet 1s -> leave"""
    if state in ("listening", "sending", "heard", "working"):
        return True
    if state == "idle":
        return False
    if since_done > _DONE_MAX_S:
        return False
    if speaking:
        return True
    if not ever_spoke:
        return since_done < _DONE_WAIT_FOR_SPEECH_S
    return quiet_for < _DONE_GAP_S


def interpret_caption(data: dict, t0: float) -> tuple[str, float, str] | None:
    """(state, caption time, card text) for one caption-file read, or None when the
    caption belongs to an EARLIER take.

    A caption older than this window (minus 1s of grace) is a previous take's: its
    "idle" would close the card the instant it opens, its "done" would show a stale
    answer. The press itself writes "listening" a moment BEFORE the window exists,
    which is what the 1s of grace is for."""
    cap_time = float(data.get("time", 0))
    text, state = (data.get("text") or ""), (data.get("state") or "")
    # Only "listening" gets the 1s grace: the press writes it a moment before the
    # window exists. A previous take's "done" or "idle" written less than a second
    # before this window opened is still the previous take -- accepting it showed
    # the last answer as if it were the new one (review round 3, finding 7).
    if state in ("done", "idle"):
        if cap_time < t0:
            return None
    elif cap_time < t0 - 1.0:
        return None
    if state == "idle":
        new = ""
    elif state == "done":
        new = f"Done:\n{text[:1200]}" if text else "Done."
    elif state in ("sending", "listening") or not text:
        new = "Listening — hold L+R, speak, release."
    elif state == "working":
        new = f"Working on:\n{text}"
    else:
        new = f"Heard — sending to agent:\n{text}"
    return (state or "listening", cap_time, new)


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

    loop = GLib.MainLoop()
    _state: dict[str, Any] = {"name": "listening", "t": t0}

    def _refresh() -> bool:
        # Change-guarded (2026-10-31): re-setting identical card text every
        # 500ms caused relayout flicker ("scrolling credits" glitch).
        now = _time.time()
        try:
            data = _json.loads(cap_path.read_text())
            view = interpret_caption(data, t0)
            if view is None:
                raise ValueError("caption from an earlier take")
            state, cap_time, new = view
            _state["name"], _state["t"] = state, cap_time
            # A NEW "done" resets its clock whatever its text is: an empty "done" used
            # to skip this, leaving the clock at zero and the card up forever.
            if state == "done" and _done["cap_time"] != cap_time:
                _done.update(at=now, cap_time=cap_time, spoke=False, last_speech=0.0)
            if new and (not _refresh_last or _refresh_last[0] != new):
                _refresh_last.clear()
                _refresh_last.append(new)
                overlay.set_card_text(new, prose=(state == "done"))
        except Exception:
            pass
        name = _state["name"]
        speaking = tts_playing() if name == "done" else False
        if speaking:
            _done["spoke"] = True
            _done["last_speech"] = now
        since_done = now - _done["at"] if _done["at"] else 0.0
        quiet_for = now - _done["last_speech"] if _done["last_speech"] else 0.0
        stay = should_stay_open(name, speaking, since_done, _done["spoke"], quiet_for)
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
