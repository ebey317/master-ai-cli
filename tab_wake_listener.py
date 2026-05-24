#!/usr/bin/env python3
"""Tab-agnostic wake listener — pixel-diff a screen region, type [WAKE] into a
target window when the region's content stops changing.

Generic replacement / extension for bc-wake-listener.py. Works for any tab
(Claude.ai, Gemini AI Mode chrome://, ChatGPT, anything) because it operates
on pixels, not on extension-provided notifications.

Usage:
    tab_wake_listener.py \\
        --region X,Y,W,H \\
        --window-id 0xWWWWWWWW \\
        [--interval 3] [--stable-ticks 2]

Behavior:
    1. Every <interval> seconds, scrot the region to a temp file, sha256 it.
    2. Track hash history. When hash CHANGES and then HOLDS for <stable-ticks>
       consecutive polls, fire wake.
    3. Wake = `xdotool type --window <wid> "[WAKE]"` + Return into the named
       Claude Code terminal. To Claude Code that looks like user input — it
       processes the turn.
    4. Log every state transition to /tmp/tab_wake_log.jsonl.

Designed to be killed cleanly with SIGTERM (e.g. `workflow off`).
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

LOG = "/tmp/tab_wake_log.jsonl"
SENTINEL = "/tmp/ai_reply_ready"
PIDFILE = "/tmp/tab_wake_listener.pid"
SHOT = "/tmp/tab_wake_shot.png"


def log(event: dict) -> None:
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def region_hash(region: str) -> str | None:
    try:
        subprocess.run(
            ["scrot", "-a", region, "-o", SHOT],
            check=True, capture_output=True, timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    try:
        with open(SHOT, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def fire_wake(window_id: str) -> None:
    """Type [WAKE] + Return into the target window."""
    try:
        subprocess.run(
            ["xdotool", "type", "--window", window_id, "[WAKE]"],
            check=True, capture_output=True, timeout=5,
        )
        subprocess.run(
            ["xdotool", "key", "--window", window_id, "Return"],
            check=True, capture_output=True, timeout=5,
        )
        try:
            with open(SENTINEL, "w") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
        except OSError:
            pass
        # Audio/speech notifications removed — wake is a signal for Claude,
        # not for the operator. Use speak.sh only when asking the operator a question.
        log({"event": "wake_fired", "window": window_id})
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log({"event": "wake_failed", "error": str(e)})


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True, help="X,Y,W,H of screen region to watch")
    p.add_argument("--window-id", required=True, help="Target window ID for [WAKE] injection (e.g. 0x04a00006)")
    p.add_argument("--interval", type=float, default=3.0, help="Seconds between polls (default 3.0)")
    p.add_argument("--stable-ticks", type=int, default=2, help="Consecutive identical polls after change to call 'stable' (default 2)")
    args = p.parse_args()

    try:
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()) + "\n")
    except OSError:
        pass

    def cleanup(signum, frame):
        log({"event": "stopped", "signal": signum})
        try:
            os.unlink(PIDFILE)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    log({"event": "started", "region": args.region, "window": args.window_id,
         "interval": args.interval, "stable_ticks": args.stable_ticks})

    last_hash = None
    state = "idle"       # idle -> changing -> firing-pending
    stable_count = 0

    while True:
        h = region_hash(args.region)
        if h is None:
            time.sleep(args.interval)
            continue

        if last_hash is None:
            last_hash = h
            state = "idle"
        elif h != last_hash:
            # Content changed — Gemini is streaming/typing.
            state = "changing"
            stable_count = 0
            last_hash = h
        else:
            # Hash matches previous.
            if state == "changing":
                stable_count += 1
                if stable_count >= args.stable_ticks:
                    fire_wake(args.window_id)
                    state = "idle"
                    stable_count = 0
            # else: still idle, no action.

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
