#!/usr/bin/env python3
"""Headless-Chrome end-to-end for Codex's BROWSER_DRIVE_INSPECT_FOLDER
handler in sensei_extension/content_script.js.

Loads Drive-shaped fixture pages directly (no real Drive account
required), mocks chrome.runtime so content_script.js's IIFE
registration doesn't throw outside extension context, then dispatches
SENSEI_EXECUTE_ACTION through the captured listener exactly the way
side_panel.js does at runtime.

Two fixtures:
  fake_drive_search.html        — Drive search results with 3 folders
                                  (incl. "07_Resume-Career") + 2 files.
  fake_drive_empty_folder.html  — Drive empty-state for a folder that
                                  contains nothing ("Drop files here").

Verifies:
  1. driveItemCandidates extracts the visible items (folders + files
     with their aria-labels and selectors).
  2. The Resume folder is recognizable in the items list (the model's
     next round would BROWSER_DOUBLE_CLICK that item to open).
  3. driveEmptyReason fires on the empty-folder fixture so DONE
     DISCIPLINE can report "the folder is empty" truthfully.

Uses the same stdlib WebSocket CDP client as test_chrome_headless_e2e.py.

Run: python3 ~/scripts/test_drive_inspect_handler.py
"""
import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WS:
    def __init__(self, url, timeout=15):
        import urllib.parse
        u = urllib.parse.urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path + ("?" + u.query if u.query else "") or "/"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        self._buf = b""
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("server closed during handshake")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        if b" 101 " not in head:
            raise RuntimeError(f"WS handshake failed: {head[:200]!r}")
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_MAGIC).encode()).digest()
        ).decode()
        if expected.lower() not in head.decode("latin-1").lower():
            raise RuntimeError("WS handshake missing Sec-WebSocket-Accept")
        self._buf = rest

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise RuntimeError("server closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, s):
        payload = s.encode()
        header = bytes([0x81])
        n = len(payload)
        mask = secrets.token_bytes(4)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < (1 << 16):
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + masked)

    def recv_text(self):
        while True:
            b0, b1 = self._read_exact(2)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            mkey = self._read_exact(4) if masked else b""
            payload = self._read_exact(n)
            if masked:
                payload = bytes(b ^ mkey[i % 4] for i, b in enumerate(payload))
            if opcode == 0x1:
                return payload.decode()
            if opcode == 0x9:
                self.sock.sendall(bytes([0x8A, 0x00]))
                continue
            if opcode == 0x8:
                raise RuntimeError("server closed")

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + secrets.token_bytes(4))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self.msg_id = 0

    def call(self, method, params=None):
        self.msg_id += 1
        self.ws.send_text(json.dumps({"id": self.msg_id, "method": method,
                                      "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == self.msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}

    def wait_for(self, event_name, timeout=15):
        deadline = time.monotonic() + timeout
        self.ws.sock.settimeout(timeout)
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(f"timeout waiting for {event_name}")
            try:
                msg = json.loads(self.ws.recv_text())
            except socket.timeout:
                raise RuntimeError(f"timeout waiting for {event_name}")
            if msg.get("method") == event_name:
                return msg.get("params") or {}

    def close(self):
        self.ws.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_chrome(port, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=1
            ) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, ConnectionRefusedError):
            time.sleep(0.2)
    raise RuntimeError(f"Chrome --remote-debugging-port={port} never came up")


def _run_handler_on_fixture(fixture_url, action):
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        raise unittest.SkipTest("no google-chrome on PATH")

    port = _free_port()
    user_data = tempfile.mkdtemp(prefix="sensei-drive-cdp-")
    proc = subprocess.Popen(
        [
            chrome, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        _wait_for_chrome(port)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=2
        ) as r:
            tabs = json.loads(r.read())
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            raise RuntimeError(f"no page tabs: {tabs}")
        ws_url = page_tabs[0]["webSocketDebuggerUrl"]

        cdp = CDP(ws_url)
        try:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Page.navigate", {"url": fixture_url})
            cdp.wait_for("Page.loadEventFired", timeout=15)
            time.sleep(0.3)
            # Sanity: confirm content_script registered the listener.
            sanity = cdp.call("Runtime.evaluate", {
                "expression": "Boolean(window.__senseiCapturedListener)",
                "returnByValue": True,
            })
            if not (sanity.get("result") or {}).get("value"):
                raise RuntimeError(
                    "content_script listener not captured — "
                    "fixture script load or IIFE init failed"
                )
            # Dispatch the action through the captured listener exactly
            # like side_panel.js does at runtime.
            expr = (
                "window.__senseiDispatch("
                + json.dumps(action)
                + ")"
            )
            res = cdp.call("Runtime.evaluate", {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
            })
            value = (res.get("result") or {}).get("value")
            if value is None:
                raise RuntimeError(f"dispatch returned no value: {res}")
            return value
        finally:
            cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(user_data, ignore_errors=True)


def _run_context_on_fixture(fixture_url, options):
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        raise unittest.SkipTest("no google-chrome on PATH")

    port = _free_port()
    user_data = tempfile.mkdtemp(prefix="sensei-context-cdp-")
    proc = subprocess.Popen(
        [
            chrome, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        _wait_for_chrome(port)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=2
        ) as r:
            tabs = json.loads(r.read())
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            raise RuntimeError(f"no page tabs: {tabs}")
        ws_url = page_tabs[0]["webSocketDebuggerUrl"]

        cdp = CDP(ws_url)
        try:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Page.navigate", {"url": fixture_url})
            cdp.wait_for("Page.loadEventFired", timeout=15)
            time.sleep(1.2)
            sanity = cdp.call("Runtime.evaluate", {
                "expression": "Boolean(window.__senseiCapturedListener)",
                "returnByValue": True,
            })
            if not (sanity.get("result") or {}).get("value"):
                raise RuntimeError("content_script listener not captured")
            expr = "window.__senseiContext(" + json.dumps(options) + ")"
            res = cdp.call("Runtime.evaluate", {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
            })
            value = (res.get("result") or {}).get("value")
            if value is None:
                raise RuntimeError(f"context returned no value: {res}")
            return value
        finally:
            cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(user_data, ignore_errors=True)


SEARCH_URL = "file:///home/user/scripts/sensei_extension/test/fake_drive_search.html"
EMPTY_URL = "file:///home/user/scripts/sensei_extension/test/fake_drive_empty_folder.html"
FILE_UPLOAD_URL = "file:///home/user/scripts/sensei_extension/test/file_upload_smoke.html"
PAGE_CONTEXT_URL = "file:///home/user/scripts/sensei_extension/test/page_context_shadow_spa_iframe.html"
FRAMEWORK_FILL_URL = "file:///home/user/scripts/sensei_extension/test/framework_fill_smoke.html"

INSPECT_ACTION = {
    "kind": "BROWSER_DRIVE_INSPECT_FOLDER",
    "target": json.dumps({"query": "resume",
                          "variants": ["Resume", "resume", "résumé", "CV", "career"]}),
}


class DriveInspectHandlerTests(unittest.TestCase):
    def test_page_context_covers_shadow_iframes_and_spa_mutation(self):
        result = _run_context_on_fixture(PAGE_CONTEXT_URL, {
            "includeVisibleText": True,
            "includeInteractiveElements": True,
            "waitForStableMs": 650,
            "maxWaitMs": 4000,
        })
        print("\n=== PAGE-CONTEXT RESULT ===")
        print(json.dumps({
            "ok": result.get("ok"),
            "title": (result.get("page_context") or {}).get("title"),
            "observe_state": (result.get("page_context") or {}).get("observe_state"),
            "iframes": (result.get("page_context") or {}).get("iframes"),
            "interactive_elements": (result.get("page_context") or {}).get("interactive_elements"),
        }, indent=2)[:2500])

        self.assertTrue(result.get("ok"), f"context returned ok=false: {result}")
        ctx = result.get("page_context") or {}
        interactive = ctx.get("interactive_elements") or ""
        self.assertIn("Shadow Apply", interactive,
                      "open shadow DOM button should be visible to the page reader")
        visible = ctx.get("visible_text") or ""
        self.assertIn("SPA loaded application step", visible,
                      "debounced MutationObserver should let URL-unchanged SPA content settle")
        obs = ctx.get("observe_state") or {}
        self.assertGreater(int(obs.get("version") or 0), 0,
                           "observe_state.version should increment after ready/mutation triggers")
        self.assertIn("main-content MutationObserver debounced", obs.get("triggers") or [])
        iframes = ctx.get("iframes") or []
        self.assertTrue(any(frame.get("same_origin") for frame in iframes),
                        f"same-origin iframe summary missing: {iframes}")
        self.assertTrue(any(frame.get("cross_origin") for frame in iframes),
                        f"cross-origin iframe metadata missing: {iframes}")

    def test_search_results_extracts_resume_folder(self):
        result = _run_handler_on_fixture(SEARCH_URL, INSPECT_ACTION)
        print("\n=== SEARCH-PAGE RESULT ===")
        print(json.dumps({
            k: (v if not isinstance(v, dict) else
                {kk: (vv if kk != "visible_text" else "<truncated>") for kk, vv in v.items()})
            for k, v in result.items()
        }, indent=2)[:2000])

        self.assertTrue(result.get("ok"),
                        f"handler returned ok=false: {result}")
        state = result.get("drive_state") or {}
        self.assertTrue(state.get("is_drive") is not None,
                        "drive_state should carry is_drive flag")
        self.assertFalse(state.get("empty"),
                         "search-results page should not be flagged empty")

        items = state.get("items") or []
        self.assertGreaterEqual(len(items), 3,
                                f"expected 3+ items on search page, got {len(items)}: "
                                f"{[i.get('name') for i in items]}")

        # The model's next round uses the items list to find + open the
        # résumé folder. Verify the specific row is reachable.
        names = [str(i.get("name") or "") for i in items]
        resume_match = next((i for i in items
                             if "resume" in (i.get("name") or "").lower()
                             or "resume" in (i.get("aria_label") or "").lower()),
                            None)
        self.assertIsNotNone(resume_match,
                             f"no 'resume' item in {names}")
        self.assertTrue(resume_match.get("selector"),
                        f"matched item missing selector: {resume_match}")
        print(f"  ✓ resume-row matched: {resume_match.get('name')!r} "
              f"selector={resume_match.get('selector')!r}")

    def test_empty_folder_detects_drop_files_here(self):
        result = _run_handler_on_fixture(EMPTY_URL, INSPECT_ACTION)
        print("\n=== EMPTY-FOLDER RESULT ===")
        print(json.dumps({
            k: (v if not isinstance(v, dict) else
                {kk: (vv if kk != "visible_text" else "<truncated>") for kk, vv in v.items()})
            for k, v in result.items()
        }, indent=2)[:1500])

        self.assertTrue(result.get("ok"),
                        f"handler returned ok=false: {result}")
        state = result.get("drive_state") or {}
        self.assertTrue(state.get("empty"),
                        "empty-folder page should set drive_state.empty=true")
        reason = (state.get("empty_reason") or "").lower()
        self.assertTrue(
            "drop files here" in reason or "new" in reason or "empty" in reason,
            f"empty_reason should describe the empty-state copy, got {reason!r}"
        )
        print(f"  ✓ empty-folder detection: reason={state.get('empty_reason')!r}")

    def test_file_upload_lands_local_file_payload(self):
        payload = b"%PDF-1.4\n% Sensei smoke upload\n"
        action = {
            "kind": "BROWSER_FILL",
            "target": "#resume :: file:///tmp/sensei-smoke-resume.pdf",
            "extras": {
                "fileUpload": {
                    "path": "/tmp/sensei-smoke-resume.pdf",
                    "name": "sensei-smoke-resume.pdf",
                    "mime": "application/pdf",
                    "size": len(payload),
                    "base64": base64.b64encode(payload).decode("ascii"),
                }
            },
        }
        result = _run_handler_on_fixture(FILE_UPLOAD_URL, action)
        print("\n=== FILE-UPLOAD RESULT ===")
        print(json.dumps(result, indent=2)[:1500])

        self.assertTrue(result.get("ok"),
                        f"handler returned ok=false: {result}")
        upload = result.get("file_upload") or {}
        self.assertEqual(upload.get("file_name"), "sensei-smoke-resume.pdf")
        self.assertEqual(upload.get("file_size"), len(payload))
        self.assertEqual(upload.get("mime"), "application/pdf")
        print(f"  ✓ file-upload landed: {upload.get('file_name')}")

    def test_framework_fill_dispatches_input_and_change_events(self):
        action = {
            "kind": "BROWSER_FILL",
            "target": "#firstName :: Elijah",
        }
        result = _run_handler_on_fixture(FRAMEWORK_FILL_URL, action)
        print("\n=== FRAMEWORK-FILL RESULT ===")
        print(json.dumps(result, indent=2)[:1500])

        self.assertTrue(result.get("ok"),
                        f"handler returned ok=false: {result}")
        framework = result.get("frameworkState") or {}
        self.assertEqual(framework.get("value"), "Elijah")
        self.assertGreaterEqual(int(framework.get("inputEvents") or 0), 1)
        self.assertGreaterEqual(int(framework.get("changeEvents") or 0), 1)
        print("  ✓ framework fill updated native value and dispatched events")


if __name__ == "__main__":
    unittest.main(verbosity=2)
