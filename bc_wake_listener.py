#!/usr/bin/env python3
"""BC-wake listener.

Reads dbus-monitor on org.freedesktop.Notifications.Notify, coalesces each
notification's multi-line block, filters to Chrome/Claude/Anthropic, and
emits ONE stdout line per match. Also touches /tmp/bc_reply_ready and
appends to /tmp/bc_wake_log.jsonl for offline inspection.

Designed to be piped from `dbus-monitor --session
"interface='org.freedesktop.Notifications',member='Notify'"`.

Spec: project_hook_design_spec_for_rebuild.md.
"""

import json
import os
import re
import sys
import time

SENTINEL = "/tmp/bc_reply_ready"
LOG = "/tmp/bc_wake_log.jsonl"

# Match strings that identify AI surfaces in our active work loop.
# Widen this when adding a new AI to AI_WATCHLIST in sensei_extension's
# service_worker.js — both must stay in sync.
FILTER = re.compile(
    r"\b(Claude|Anthropic|claude\.ai|DeepSeek|BC|Gemini|ChatGPT|GPT-?[0-9o]|aistudio|Bard|Copilot)\b",
    re.IGNORECASE,
)

# dbus-monitor blocks start with `method call` or `signal` and are separated
# by blank lines. Each block carries the Notify args as a flat list of
# typed values: string app_name, uint32 replaces_id, string app_icon,
# string summary, string body, array actions, dict hints, int32 expire.

_STRING_RE = re.compile(r'^\s+string "(.*)"\s*$')


def flush_block(lines: list[str]) -> None:
    """Parse one Notify call block. Emit one line if it matches FILTER."""
    if not lines:
        return
    if "member=Notify" not in lines[0] and "Notify" not in lines[0]:
        return
    strings = []
    for ln in lines[1:]:
        m = _STRING_RE.match(ln)
        if m:
            strings.append(m.group(1))
    # Notify signature: app_name, app_icon, summary, body. The icon is a
    # string between app_name and summary; index varies if hints reorder.
    # Take first 4 strings and pick by position.
    app_name = strings[0] if len(strings) > 0 else ""
    # app_icon is index 1; summary is index 2; body is index 3.
    summary = strings[2] if len(strings) > 2 else ""
    body = strings[3] if len(strings) > 3 else ""
    haystack = f"{app_name} | {summary} | {body}"
    if not FILTER.search(haystack):
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    event = {
        "ts": ts,
        "app": app_name,
        "summary": summary,
        "body": body,
    }
    try:
        with open(LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass
    try:
        # Touch sentinel so other listeners (inotifywait) can chain.
        with open(SENTINEL, "w") as f:
            f.write(ts + "\n")
    except OSError:
        pass
    # Single coalesced line for the Monitor stream.
    out = f"WAKE {ts} app={app_name!r} summary={summary!r} body={body[:120]!r}"
    print(out, flush=True)


def main() -> int:
    block: list[str] = []
    # Use readline() in a loop instead of `for line in sys.stdin` to avoid
    # Python's iterator-level input buffering, which holds a notification
    # in the pipe until the *next* one arrives.
    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        line = raw.rstrip("\n")
        if not line.strip():
            flush_block(block)
            block = []
            continue
        # New block begins with `method call` or `signal` at column 0.
        if line and not line[0].isspace() and block:
            flush_block(block)
            block = [line]
        else:
            block.append(line)
        # The last arg of Notify is `int32 <expire>`. When we see that, the
        # block is complete — flush immediately so events emit in real time
        # instead of waiting for the next notification to push them out.
        if line.lstrip().startswith("int32 ") and block:
            flush_block(block)
            block = []
    flush_block(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
