#!/usr/bin/env python3
"""Local reverse proxy in front of OpenCode Go (https://opencode.ai/zen/go/v1).

Why this exists: OpenCode Go's backend validates that a request looks like it
came from a known client — a stable `x-opencode-session` header and a
specific User-Agent — before it will bill against the $10/mo subscription's
included quota. The generic `opencode` CLI (what no-mistakes shells out to
as its pipeline agent) doesn't send those, so every request from it gets
rejected with a generic "Insufficient balance" error even though the account
has quota. master-ai-cli's own Python client (master_ai.py, _ask_opencode_go)
already sends the exact header shape the backend accepts — confirmed live via
a direct curl replicating those headers, which succeeded with "cost":"0".

Rather than get the opencode CLI itself to send those headers (out of our
control — it's a third-party npm package), this proxy sits between the CLI
and the real endpoint: it rewrites only the identifying headers on every
request, forwards the body completely untouched (including tool-calls and
`stream: true`), and streams the upstream response straight back — so the
CLI never has to know anything changed.

Usage: point opencode's custom provider baseURL at this proxy
(http://127.0.0.1:<port>/v1) instead of https://opencode.ai/zen/go/v1
directly. The API key opencode sends doesn't matter — this always injects
the real one server-side.
"""

import http.client
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPSTREAM_HOST = "opencode.ai"
UPSTREAM_BASE_PATH = "/zen/go/v1"
SESSION_ID = "no-mistakes-relay"
USER_AGENT = "master-ai-cli/1.0 (Sensei agent loop)"
PORT = int(os.environ.get("OPENCODE_GO_RELAY_PORT", "8793"))

KEYCHAIN_FILE = Path.home() / "Desktop" / "Projects" / "keychain" / "master_ai_keys"


def _load_key() -> str:
    """Same keychain master-ai-cli itself reads from (KEYS.get('opencode_go'))."""
    try:
        for line in KEYCHAIN_FILE.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "opencode" and parts[1] == "go":
                return parts[2].strip()
    except Exception as e:
        print(
            f"[opencode_go_relay] FATAL: could not read keychain: {e}", file=sys.stderr
        )
    return ""


API_KEY = _load_key()
if not API_KEY:
    print(
        "[opencode_go_relay] FATAL: no opencode_go key found in keychain",
        file=sys.stderr,
    )
    sys.exit(1)


class RelayHandler(BaseHTTPRequestHandler):
    def _forward(self):
        # Everything after /v1 maps onto UPSTREAM_BASE_PATH — so /v1/chat/completions
        # -> /zen/go/v1/chat/completions, /v1/models -> /zen/go/v1/models, etc.
        suffix = self.path[len("/v1") :] if self.path.startswith("/v1") else self.path
        upstream_path = UPSTREAM_BASE_PATH + suffix

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else None

        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "x-opencode-session": SESSION_ID,
            "User-Agent": USER_AGENT,
            "Accept": self.headers.get("Accept", "application/json"),
        }
        if body:
            headers["Content-Type"] = self.headers.get(
                "Content-Type", "application/json"
            )
            headers["Content-Length"] = str(len(body))

        conn = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=300)
        try:
            conn.request(self.command, upstream_path, body=body, headers=headers)
            resp = conn.getresponse()

            self.send_response(resp.status)
            for name, value in resp.getheaders():
                if name.lower() in (
                    "connection",
                    "transfer-encoding",
                    "content-encoding",
                ):
                    continue
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()

            # Stream the response back as it arrives (SSE-friendly: opencode's own
            # `stream: true` chat requests depend on this not being buffered whole).
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        except Exception as e:
            print(f"[opencode_go_relay] upstream error: {e}", file=sys.stderr)
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": str(e)}}).encode())
            except Exception:
                pass
        finally:
            conn.close()

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def log_message(self, fmt, *args):
        print(
            f"[opencode_go_relay] {self.address_string()} {fmt % args}", file=sys.stderr
        )


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), RelayHandler)
    print(
        f"[opencode_go_relay] listening on http://127.0.0.1:{PORT}/v1 -> https://{UPSTREAM_HOST}{UPSTREAM_BASE_PATH}",
        file=sys.stderr,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
