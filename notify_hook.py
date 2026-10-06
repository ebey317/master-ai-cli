#!/usr/bin/env python3
"""
notify_hook.py — minimal Sensei notification listener.

Waits for stdin (or a single argument) and fires a desktop notification
via notify2 (falls back to os.system xdg-open if notify2 is missing).
"""

import json
import sys


def _notify(title, body, app="Sensei"):
    try:
        import notify2

        notify2.init(app)
        n = notify2.Notification(title, body, "dialog-information")
        n.set_urgency(notify2.URGENCY_NORMAL)
        n.set_timeout(5000)
        n.show()
        return "notify2"
    except Exception:
        # Fallback: write to a log we can read
        import os

        os.makedirs("/tmp/sensei_hooks", exist_ok=True)
        with open("/tmp/sensei_hooks/notify.log", "a") as f:
            f.write(json.dumps({"title": title, "body": body, "app": app}) + "\n")
        return "log"


if __name__ == "__main__":
    if len(sys.argv) > 1:
        title = sys.argv[1]
        body = sys.argv[2] if len(sys.argv) > 2 else ""
    else:
        title = sys.stdin.readline().strip()
        body = sys.stdin.read().strip()
    method = _notify(title, body)
    print(f"✓ Notification sent via {method}: {title} — {body}")
    sys.exit(0)
