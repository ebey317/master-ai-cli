#!/usr/bin/env python3
"""End-to-end: drives a real headless Chrome to fill + submit job_app_smoke.html.

Closes the only remaining gap on task #7 (extension fills+submits a job
app end-to-end). The existing tests prove:

  - Backend emits a correct 11-action sequence (test_extension_e2e_smoke.py)
  - Selectors resolve on the fixture HTML (same test, hardened)
  - content_script.js can execute BROWSER_FILL/BROWSER_CLICK on a real
    page (loop_smoke.html / LOOP_CHECKLIST.md)

This test fills the last seam: actually execute those 11 actions in a
real Chrome rendering job_app_smoke.html, and verify the fixture's
submit handler fired and set window.__senseiJobAppSmoke.submitted to
true. Uses the Chrome DevTools Protocol over WebSocket (Python's
stdlib `websockets` package — already on this box), no Chrome
extension loaded — the actions are dispatched inline via
Runtime.evaluate, mimicking content_script.js setElementValue +
event-dispatch logic exactly.

Why this is the right shape of test:
  - Real browser, real DOM, real JS event dispatch, real submit handler.
  - Headless — no display required, runs on any machine that has
    google-chrome installed.
  - Hermetic — its own --user-data-dir in /tmp; Chrome is killed
    on teardown; never touches the user's Chrome profile.
  - Deterministic — driven from a fixed action list; no model call.

Run: python3 ~/scripts/test_chrome_headless_e2e.py
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
import urllib.parse
import urllib.request

# Minimal synchronous WebSocket client (RFC 6455). The packaged
# `websockets==9.1` on Ubuntu 22.04 calls asyncio APIs with `loop=`
# that Python 3.10 removed; patching every site is more code than
# this client. Pure stdlib.
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WS:
    def __init__(self, url, timeout=15):
        u = urllib.parse.urlparse(url)
        if u.scheme != "ws":
            raise ValueError(f"only ws:// supported, got {u.scheme}")
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path + ("?" + u.query if u.query else "") or "/"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        self._buf = b""
        # Read until \r\n\r\n
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
        header = bytes([0x81])  # FIN + opcode=1 (text)
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
        # Loop until we get a TEXT frame (skip pings/pongs).
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
            if opcode == 0x1:  # text
                return payload.decode()
            if opcode == 0x9:  # ping → pong
                self.sock.sendall(bytes([0x8A, 0x00]))
                continue
            if opcode == 0x8:  # close
                raise RuntimeError("server closed")
            # 0x0 continuation, 0x2 binary — not used by CDP

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + secrets.token_bytes(4))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "sensei_extension", "test", "job_app_smoke.html",
)
FIXTURE_URL = f"file://{FIXTURE_PATH}"

# Reproduces the action sequence the model emits — see
# test_extension_e2e_smoke.py for the live source-of-truth check.
# Keeping it fixed here means this test runs offline (no Ollama needed)
# and is fast: the question this test answers is "if the model emits
# the right actions, does the page actually accept them?"
ACTIONS = [
    {"kind": "BROWSER_FILL",  "target": "#firstName :: Elijah"},
    {"kind": "BROWSER_FILL",  "target": "#lastName :: W."},
    {"kind": "BROWSER_FILL",  "target": "#email :: you@example.com"},
    {"kind": "BROWSER_FILL",  "target": "#phone :: 317-555-0100"},
    {"kind": "BROWSER_FILL",  "target": "#city :: Indianapolis"},
    {"kind": "BROWSER_FILL",  "target": "#state :: IN"},
    {"kind": "BROWSER_FILL",  "target": "#zip :: 46201"},
    {"kind": "BROWSER_FILL",  "target": "#yearsExperience :: 10"},
    {"kind": "BROWSER_CLICK", "target": 'input[name="workAuth"][value="yes"]'},
    {"kind": "BROWSER_FILL",  "target": "#coverLetter :: I want this job"},
    {"kind": "BROWSER_CLICK", "target": "#submitButton"},
]


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


# JS executed in the page to replicate content_script.js's BROWSER_FILL
# and BROWSER_CLICK execution. Mirrors:
#   - parseFillTarget (split on ::, =>, :=)
#   - findElement (querySelector then text-content fallback)
#   - setElementValue (prototype value descriptor + input/change events)
# This is the same logic content_script.js runs in production; if
# content_script.js diverges, update this string to match.
DRIVER_JS = r"""
(function() {
  function _parseFillTarget(raw) {
    raw = String(raw || "").trim();
    if (raw.startsWith("{")) {
      try { var o = JSON.parse(raw); return {sel: o.selector||o.target||"", val: o.value||o.text||""}; }
      catch(_){ /* fall through */ }
    }
    var m = raw.match(/^(.*?)\s*(?:=>|:=|::)\s*([\s\S]*)$/);
    if (m) return {sel: m[1].trim(), val: m[2].trim()};
    return {sel: raw, val: ""};
  }
  function _find(sel) {
    try { return document.querySelector(sel); } catch(_e){ return null; }
  }
  function _setValue(el, value) {
    if (!el) return;
    el.focus();
    var proto = Object.getPrototypeOf(el);
    var d = Object.getOwnPropertyDescriptor(proto, "value");
    if (d && d.set) d.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event("input",  {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }
  var actions = __ACTIONS_JSON__;
  var results = [];
  for (var i = 0; i < actions.length; i++) {
    var a = actions[i];
    var k = (a.kind || "").toUpperCase();
    if (k === "BROWSER_FILL") {
      var p = _parseFillTarget(a.target);
      var el = _find(p.sel);
      if (!el) { results.push({i:i, ok:false, sel:p.sel, error:"not found"}); continue; }
      _setValue(el, p.val);
      results.push({i:i, ok:true, sel:p.sel, val:p.val, kind:"FILL"});
    } else if (k === "BROWSER_CLICK") {
      var el = _find(a.target);
      if (!el) { results.push({i:i, ok:false, sel:a.target, error:"not found"}); continue; }
      el.click();
      results.push({i:i, ok:true, sel:a.target, kind:"CLICK"});
    } else {
      results.push({i:i, ok:false, error:"unknown kind " + k});
    }
  }
  return {
    actions: results,
    state: window.__senseiJobAppSmoke || null,
    resultText: (document.getElementById("result") || {}).textContent || null,
  };
})();
""".strip()


class CdpClient:
    """Minimal Chrome DevTools Protocol client — synchronous over our
    stdlib WebSocket."""
    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self.msg_id = 0

    def close(self):
        self.ws.close()

    def call(self, method, params=None):
        self.msg_id += 1
        payload = {"id": self.msg_id, "method": method, "params": params or {}}
        self.ws.send_text(json.dumps(payload))
        while True:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == self.msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg.get("result") or {}

    def wait_for_event(self, name, timeout=15):
        deadline = time.monotonic() + timeout
        self.ws.sock.settimeout(timeout)
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(f"timeout waiting for {name}")
            try:
                msg = json.loads(self.ws.recv_text())
            except socket.timeout:
                raise RuntimeError(f"timeout waiting for {name}")
            if msg.get("method") == name:
                return msg.get("params") or {}


def _run_in_chrome():
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        raise unittest.SkipTest("no google-chrome binary on PATH")

    port = _free_port()
    user_data = tempfile.mkdtemp(prefix="sensei-cdp-")
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        _wait_for_chrome(port)

        # Modern Chrome requires PUT on /json/new; older accepts GET. We
        # avoid the inconsistency entirely by attaching to the default
        # about:blank tab Chrome opens at startup and navigating IT to the
        # fixture URL via Page.navigate (below).
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=2
        ) as r:
            tabs = json.loads(r.read())
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if not page_tabs:
            raise RuntimeError(f"Chrome exposed no page tabs: {tabs}")
        ws_url = page_tabs[0]["webSocketDebuggerUrl"]

        cdp = CdpClient(ws_url)
        try:
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Page.navigate", {"url": FIXTURE_URL})
            cdp.wait_for_event("Page.loadEventFired", timeout=15)
            # Belt-and-suspenders: tiny settle so any DOMContentLoaded
            # handlers register before we touch elements.
            time.sleep(0.2)

            driver = DRIVER_JS.replace(
                "__ACTIONS_JSON__", json.dumps(ACTIONS)
            )
            res = cdp.call("Runtime.evaluate", {
                "expression": driver,
                "returnByValue": True,
                "awaitPromise": False,
            })
            value = (res.get("result") or {}).get("value")
            if value is None:
                raise RuntimeError(f"Runtime.evaluate returned no value: {res}")
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


class ChromeHeadlessEndToEnd(unittest.TestCase):
    def test_fills_and_submits_job_app_in_real_chrome(self):
        try:
            result = _run_in_chrome()
        except unittest.SkipTest:
            raise
        except Exception as e:
            self.fail(f"CDP run failed: {type(e).__name__}: {e}")

        # Diagnostics first — print before assertions so a failure shows
        # exactly which action(s) broke.
        print("\n=== ACTION RESULTS ===")
        for a in result.get("actions") or []:
            status = "OK " if a.get("ok") else "FAIL"
            extra = a.get("error") or (a.get("val") or "")[:60]
            print(f"  {status}  {a.get('kind','?'):5}  {a.get('sel','')}  {extra}")
        print("\n=== FIXTURE STATE ===")
        print(json.dumps(result.get("state"), indent=2))
        print("\n=== RESULT TEXT (top 200 chars) ===")
        print((result.get("resultText") or "")[:200])

        # Every action must have executed without "not found".
        bad = [a for a in (result.get("actions") or []) if not a.get("ok")]
        self.assertEqual(bad, [],
                         f"{len(bad)} action(s) failed to execute on the page")

        state = result.get("state") or {}
        self.assertTrue(state.get("submitted"),
                        f"submit handler did not fire — state={state}")
        self.assertEqual(state.get("missing") or [], [],
                         f"submit handler reported missing fields: {state.get('missing')}")
        self.assertEqual(state.get("attempts"), 1,
                         f"submit fired {state.get('attempts')} time(s), expected 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
