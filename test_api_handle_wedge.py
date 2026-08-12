#!/usr/bin/env python3
"""Regression test for the Ollama wedge fix (2026-05-14).

When the global _API_HANDLE_LOCK is held by another request (e.g., a runaway
local inference), a new /chat call must NOT block indefinitely. It must raise
ApiHandleBusy within the configured timeout so the HTTP layer can return 503
and cloud lanes / the Chrome extension can retry instead of hanging.

Run: python3 ~/scripts/test_api_handle_wedge.py
"""
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "/home/user/scripts")

import stt_server  # noqa: E402


class WedgeRegressionTests(unittest.TestCase):
    """Lock-acquire timeout path."""

    def test_busy_lock_raises_apihandlebusy_within_timeout(self):
        """When a concurrent request holds _API_HANDLE_LOCK, a second
        api_handle() call raises ApiHandleBusy within the configured timeout
        instead of blocking for the duration of the inner Ollama call."""
        # Speed up the test: shorten the timeout to 0.5s. The real prod value
        # is 120s — see _API_HANDLE_LOCK_TIMEOUT_S in stt_server.py.
        with mock.patch.object(stt_server, "_API_HANDLE_LOCK_TIMEOUT_S", 0.5):
            holder_acquired = threading.Event()
            holder_release = threading.Event()

            # api_handle() serializes per-lane (see _API_HANDLE_LOCKS) so
            # cloud lanes stop queuing behind local Ollama inference. This
            # payload (plain "hello", no model prefix) predicts to the
            # "local" lane, so that's the lock to contend for.
            def _holder():
                stt_server._API_HANDLE_LOCKS["local"].acquire()
                holder_acquired.set()
                # Hold longer than the test's acquire timeout (0.5s).
                holder_release.wait(timeout=3.0)
                stt_server._API_HANDLE_LOCKS["local"].release()

            t = threading.Thread(target=_holder, daemon=True)
            t.start()
            self.assertTrue(holder_acquired.wait(timeout=2.0),
                            "holder thread never acquired lock — test setup broken")

            start = time.monotonic()
            try:
                stt_server.api_handle({
                    "prompt": "hello",
                    "mode": "plan",
                    "source": "chrome_extension",
                    "session_id": "wedge-regression",
                })
                self.fail("expected ApiHandleBusy, got normal return")
            except stt_server.ApiHandleBusy as e:
                elapsed = time.monotonic() - start
                # Must fail fast — well under the prod 120s budget.
                self.assertLess(elapsed, 2.0,
                                f"ApiHandleBusy raised but took {elapsed:.2f}s "
                                f"(timeout was 0.5s)")
                self.assertIn("dispatch lock", str(e).lower())
            finally:
                holder_release.set()
                t.join(timeout=2.0)

    def test_uncontended_lock_does_not_raise_apihandlebusy(self):
        """Sanity check: when the lock is free, api_handle does NOT raise
        ApiHandleBusy. (It may raise other things — bad payload, missing model
        — but the wedge protection must not false-fire.)"""
        # Make sure the lock is unheld before we start.
        self.assertTrue(stt_server._API_HANDLE_LOCKS["local"].acquire(timeout=1.0),
                        "lock was already held going into the uncontended test")
        stt_server._API_HANDLE_LOCKS["local"].release()

        # Use a payload that triggers an early ValueError (missing prompt)
        # so we don't actually call Ollama in the test.
        try:
            stt_server.api_handle({"prompt": "", "mode": "plan"})
        except stt_server.ApiHandleBusy:
            self.fail("ApiHandleBusy raised on an uncontended lock — false positive")
        except Exception:
            # Any other exception (ValueError for missing prompt, model error,
            # etc.) is fine — we only care that ApiHandleBusy didn't fire.
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
