#!/usr/bin/env python3
"""BC tab-title wake relay.

Tiny localhost HTTP server. POST /wake from claude.ai's BC tab fires a
notify-send with 'Claude BC ready' which the existing bc_wake_listener
service catches via dbus -> /tmp/bc_reply_ready + JSONL. Closes the
wake-loop gap when Chrome doesn't fire a desktop notification on its own.

Listens on 127.0.0.1:8765 only.

Run: python3 /home/elijah/scripts/bc_title_relay.py
"""

import http.server
import json
import socketserver
import subprocess
import sys
import time

PORT = 8765
LAST_FIRE = {"ts": 0.0}
DEDUP_WINDOW_S = 3.0


class Handler(http.server.BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "https://claude.ai")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/wake":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        try:
            payload = json.loads(raw) if raw.startswith("{") else {"title": raw}
        except json.JSONDecodeError:
            payload = {"title": raw}
        title = (payload.get("title") or "").strip()[:160]
        ai = (payload.get("ai") or "").strip()[:32]
        now = time.time()
        if now - LAST_FIRE["ts"] < DEDUP_WINDOW_S:
            self.send_response(202)
            self._cors()
            self.end_headers()
            self.wfile.write(b"deduped")
            return
        LAST_FIRE["ts"] = now
        ai_label = {
            "claude": "Claude (BC)",
            "deepseek": "DeepSeek",
        }.get(ai, ai.title() if ai else "AI")
        summary = f"{ai_label} ready"
        body = title or f"{ai_label} tab title flipped"
        try:
            subprocess.run(
                ["notify-send", summary, body],
                check=False,
                timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            sys.stderr.write(f"notify-send failed: {e}\n")
        self.send_response(200)
        self._cors()
        self.end_headers()
        self.wfile.write(b"fired")

    def log_message(self, fmt, *args):
        sys.stderr.write("[bc-title-relay] " + fmt % args + "\n")


def main() -> int:
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as srv:
        sys.stderr.write(f"[bc-title-relay] listening on 127.0.0.1:{PORT}\n")
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
