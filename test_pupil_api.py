#!/usr/bin/env python3
"""Contract test for the Pupil HTTP API.

Verifies that the endpoints documented in ~/scripts/pupil_api.md return the
documented shapes. Does NOT assert correctness of the LLM reply or the
specific memory count — only that each endpoint returns the expected keys
with the expected types.

Run: python3 ~/scripts/test_pupil_api.py
Exit: 0 = all green, non-zero = at least one shape mismatch or error.

Requires stt_server.py running on 127.0.0.1:8080 (typically via the
master-ai-ui systemd user service).
"""

import json
import sys
import time
import unittest
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
TIMEOUT_S = 10
CHAT_TIMEOUT_S = 360  # generous — cold-start chat baseline is 300s


def _get(path: str, timeout: float = TIMEOUT_S):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
        return resp.status, json.loads(body) if body else {}


def _post(
    path: str, payload: dict, timeout: float = TIMEOUT_S, expect_status: int = 200
):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        return e.code, json.loads(text) if text else {}


class PupilAPIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            urllib.request.urlopen(BASE + "/health", timeout=2).read()
        except Exception as e:
            raise unittest.SkipTest(
                f"stt_server not reachable at {BASE} ({e}); "
                "start it via `systemctl --user restart master-ai-ui` before running."
            )

    # ---- GET /health ----

    def test_health_shape(self):
        status, body = _get("/health")
        self.assertEqual(status, 200)
        for key in ("ok", "ollama", "model", "ts"):
            self.assertIn(key, body, f"/health missing key: {key}")
        self.assertIsInstance(body["ok"], bool)
        self.assertIn(body["ollama"], ("active", "down"))
        self.assertIsInstance(body["model"], str)
        self.assertIsInstance(body["ts"], str)
        self.assertGreater(len(body["ts"]), 10, "ts should be ISO-8601-ish, not empty")

    # ---- GET /status ----

    def test_status_shape(self):
        status, body = _get("/status")
        self.assertEqual(status, 200)
        for key in (
            "mode",
            "model",
            "memory_facts",
            "last_route",
            "queue_depth",
            "loaded_models",
            "mem",
            "ts",
        ):
            self.assertIn(key, body, f"/status missing key: {key}")
        self.assertIn(body["mode"], ("plan", "review", "auto"))
        self.assertIsInstance(body["memory_facts"], int)
        self.assertIsInstance(body["queue_depth"], int)
        self.assertIsInstance(body["loaded_models"], list)
        self.assertIsInstance(body["mem"], dict)
        for mk in ("total_mb", "used_mb", "available_mb", "swap_used_mb"):
            self.assertIn(mk, body["mem"])
            self.assertIsInstance(body["mem"][mk], int)

    # ---- POST /chat ----

    def test_chat_rejects_empty_prompt(self):
        status, body = _post("/chat", {"prompt": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_chat_rejects_invalid_mode(self):
        status, body = _post("/chat", {"prompt": "hi", "mode": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_chat_happy_path_shape(self):
        # Tiny prompt so even cold-start stays under our timeout.
        status, body = _post(
            "/chat",
            {"prompt": "Reply with the single word: pong."},
            timeout=CHAT_TIMEOUT_S,
        )
        self.assertEqual(status, 200, f"unexpected body: {body}")
        for key in ("reply", "route", "model", "latency_ms", "blocked_actions", "ts"):
            self.assertIn(key, body, f"/chat missing key: {key}")
        self.assertIsInstance(body["reply"], str)
        self.assertIsInstance(body["route"], str)
        self.assertIsInstance(body["model"], str)
        self.assertIsInstance(body["latency_ms"], int)
        self.assertGreaterEqual(body["latency_ms"], 0)
        self.assertIsInstance(body["blocked_actions"], list)
        self.assertIsInstance(body["ts"], str)

    # ---- POST /mode ----

    def test_mode_rejects_invalid(self):
        status, body = _post("/mode", {"mode": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_mode_round_trip(self):
        # Capture current mode, switch through all three, restore.
        status, before = _get("/status")
        self.assertEqual(status, 200)
        original = before["mode"]
        try:
            for target in ("plan", "review", "auto"):
                status, body = _post("/mode", {"mode": target})
                self.assertEqual(status, 200)
                self.assertEqual(body, {"ok": True, "mode": target})
                _, after = _get("/status")
                self.assertEqual(
                    after["mode"], target, f"/status did not reflect mode {target}"
                )
        finally:
            _post("/mode", {"mode": original})

    # ---- POST /voice ----

    def test_voice_rejects_missing_enabled(self):
        status, body = _post("/voice", {})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_voice_rejects_non_bool_enabled(self):
        status, body = _post("/voice", {"enabled": "yes"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_voice_round_trip(self):
        for value in (True, False):
            status, body = _post("/voice", {"enabled": value})
            self.assertEqual(status, 200)
            self.assertTrue(body.get("ok"))
            self.assertEqual(body["voice_state"]["enabled"], value)
            self.assertIn("engine", body["voice_state"])

    # ---- GET /events ----

    def test_events_emits_hello_then_heartbeat_capable(self):
        # We only verify the first event (hello) so we don't have to wait
        # 15s for the first heartbeat. SSE format: blank line ends an event.
        req = urllib.request.Request(BASE + "/events")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            ct = (resp.headers.get("Content-Type") or "").lower()
            self.assertTrue(
                ct.startswith("text/event-stream"), f"unexpected Content-Type: {ct}"
            )
            # Read enough bytes to capture the first event payload.
            chunk = b""
            deadline = time.time() + 5
            while time.time() < deadline and b"\n\n" not in chunk:
                chunk += resp.read1(256)
                if b"event: hello" in chunk and b"\n\n" in chunk:
                    break
            self.assertIn(b"event: hello", chunk)
            self.assertIn(b'"ts"', chunk)

    # ---- Legacy compatibility — do not break existing endpoints ----

    def test_sys_still_responds(self):
        status, body = _get("/sys")
        self.assertEqual(status, 200)
        # /sys legacy shape: {mem, loaded_models}
        self.assertIn("mem", body)
        self.assertIn("loaded_models", body)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=2).result.wasSuccessful() else 1)
