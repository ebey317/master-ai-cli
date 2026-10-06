import os
import unittest
from pathlib import Path
from unittest.mock import patch

from verifiers import VerifyResult, verify_notification_sent


class TestVerifyNotificationSent(unittest.TestCase):
    """Tests for verify_notification_sent — covers the notification-readiness
    verifier not already covered by test_verifiers.py."""

    _FAKE_BUS_OK = "unix:path=/tmp/fake-dbus-present"
    _FAKE_BUS_MISSING = "unix:path=/tmp/fake-dbus-missing"

    # ── D-Bus socket check ────────────────────────────────────────────────

    @patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": _FAKE_BUS_MISSING})
    @patch("os.path.exists", return_value=False)
    def test_dbus_socket_missing_returns_failure(self, _mock_exists):
        result = verify_notification_sent()
        self.assertFalse(result.ok)
        self.assertIsNone(result.observed)
        self.assertIn("D-Bus session socket not found", result.reason)

    @patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": _FAKE_BUS_MISSING})
    @patch("os.path.exists", return_value=False)
    def test_dbus_socket_missing_elapsed_ms_non_negative(self, _mock_exists):
        result = verify_notification_sent()
        self.assertGreaterEqual(result.elapsed_ms, 0)

    # ── notify-send check ─────────────────────────────────────────────────

    @patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": _FAKE_BUS_OK})
    @patch.object(Path, "is_file", new=lambda self: True)
    @patch("shutil.which", return_value=None)
    @patch("os.path.exists", return_value=True)
    def test_notify_send_missing_returns_failure(self, _exists, _which):
        result = verify_notification_sent()
        self.assertFalse(result.ok)
        self.assertIsNone(result.observed)
        self.assertEqual(result.reason, "notify-send binary not found in PATH")

    # ── wrapper script check ──────────────────────────────────────────────

    @patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": _FAKE_BUS_OK})
    @patch.object(Path, "is_file", new=lambda self: False)
    @patch("shutil.which", return_value="/usr/bin/notify-send")
    @patch("os.path.exists", return_value=True)
    def test_wrapper_script_missing_returns_failure(self, _exists, _which):
        result = verify_notification_sent()
        self.assertFalse(result.ok)
        self.assertIsNone(result.observed)
        self.assertIn("sensei-notify.sh wrapper missing", result.reason)

    # ── happy path ────────────────────────────────────────────────────────

    @patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": _FAKE_BUS_OK})
    @patch.object(Path, "is_file", new=lambda self: True)
    @patch("shutil.which", return_value="/usr/bin/notify-send")
    @patch("os.path.exists", return_value=True)
    def test_all_prerequisites_satisfied(self, _exists, _which):
        result = verify_notification_sent()
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.observed)
        self.assertEqual(result.reason, "notification prerequisites satisfied")
        self.assertGreaterEqual(result.elapsed_ms, 0)

    @patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": _FAKE_BUS_OK})
    @patch.object(Path, "is_file", new=lambda self: True)
    @patch("shutil.which", return_value="/usr/bin/notify-send")
    @patch("os.path.exists", return_value=True)
    def test_observed_contains_socket_path(self, _exists, _which):
        result = verify_notification_sent()
        self.assertIn("/tmp/fake-dbus-present", result.observed)

    # ── signature / type contract ─────────────────────────────────────────

    def test_always_returns_verify_result(self):
        with patch.dict(
            os.environ, {"DBUS_SESSION_BUS_ADDRESS": self._FAKE_BUS_MISSING}
        ):
            with patch("os.path.exists", return_value=False):
                result = verify_notification_sent()
        self.assertIsInstance(result, VerifyResult)

    @patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": _FAKE_BUS_OK})
    @patch.object(Path, "is_file", new=lambda self: True)
    @patch("shutil.which", return_value="/usr/bin/notify-send")
    @patch("os.path.exists", return_value=True)
    def test_positional_arg_ignored(self, _exists, _which):
        result = verify_notification_sent("some_app_name")
        self.assertTrue(result.ok)

    @patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": _FAKE_BUS_OK})
    @patch.object(Path, "is_file", new=lambda self: True)
    @patch("shutil.which", return_value="/usr/bin/notify-send")
    @patch("os.path.exists", return_value=True)
    def test_none_arg_ignored(self, _exists, _which):
        result = verify_notification_sent(None)
        self.assertTrue(result.ok)

    # ── XDG_RUNTIME_DIR fallback (no DBUS env var) ────────────────────────

    def test_xdg_runtime_dir_fallback_when_no_dbus_env(self):
        # Empty DBUS_SESSION_BUS_ADDRESS triggers the XDG_RUNTIME_DIR fallback.
        with patch.dict(
            os.environ,
            {"DBUS_SESSION_BUS_ADDRESS": "", "XDG_RUNTIME_DIR": "/run/user/9999"},
        ):
            with patch("os.path.exists", return_value=False) as mock_exists:
                result = verify_notification_sent()
        self.assertFalse(result.ok)
        checked = [str(c.args[0]) for c in mock_exists.call_args_list if c.args]
        self.assertTrue(
            any("/run/user/9999/bus" in p for p in checked),
            f"Expected /run/user/9999/bus in os.path.exists calls; got {checked}",
        )


if __name__ == "__main__":
    unittest.main()
