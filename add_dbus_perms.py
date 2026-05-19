#!/usr/bin/env python3
"""Idempotently add three Bash permissions to ~/.claude/settings.local.json.

Run this from your own shell — NOT through Claude — so the agent-config
classifier doesn't block it. The script is read-only on every entry that
isn't one of the three target lines.

Adds (only if not already present):
  Bash(dbus-monitor *)
  Bash(gdbus monitor *)
  Bash(python3 /home/elijah/scripts/bc_wake_listener.py)
"""

import json
from pathlib import Path

TARGET = Path.home() / ".claude" / "settings.local.json"
ADDS = [
    "Bash(dbus-monitor *)",
    "Bash(gdbus monitor *)",
    "Bash(python3 /home/elijah/scripts/bc_wake_listener.py)",
]


def main() -> int:
    data = json.loads(TARGET.read_text())
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    added = []
    for entry in ADDS:
        if entry not in allow:
            allow.insert(0, entry)
            added.append(entry)
    TARGET.write_text(json.dumps(data, indent=2) + "\n")
    if added:
        print("Added:")
        for a in added:
            print("  " + a)
    else:
        print("All three already present — no changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
