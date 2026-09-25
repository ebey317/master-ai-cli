#!/usr/bin/env python3
"""Unit tests for the P2.3 read path fence.

Verifies _read_path_ok blocks:
- Secret-path patterns (~/.ssh/, /etc/shadow, ~/.aws/credentials, etc.)
- Symlink escapes (link inside HOME pointing outside allowed roots)
- Paths outside allowed roots

And allows:
- ~/scripts/*, ~/Desktop/*, /tmp/*, /var/log/*
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


class ReadFenceAllowed(unittest.TestCase):
    def test_home_scripts_allowed(self):
        ok, _ = master_ai._read_path_ok(str(Path.home() / "scripts" / "master_ai.py"))
        self.assertTrue(ok)

    def test_home_desktop_allowed(self):
        ok, _ = master_ai._read_path_ok(str(Path.home() / "Desktop" / "foo.txt"))
        self.assertTrue(ok)

    def test_tmp_allowed(self):
        ok, _ = master_ai._read_path_ok("/tmp/test-file.txt")
        self.assertTrue(ok)

    def test_var_log_allowed(self):
        ok, _ = master_ai._read_path_ok("/var/log/syslog")
        self.assertTrue(ok)

    def test_home_root_allowed(self):
        ok, _ = master_ai._read_path_ok(str(Path.home() / ".bashrc"))
        self.assertTrue(ok)


class ReadFenceSecretPaths(unittest.TestCase):
    def test_ssh_dir_denied(self):
        ok, why = master_ai._read_path_ok(str(Path.home() / ".ssh" / "id_rsa"))
        self.assertFalse(ok)
        self.assertIn("ssh", why)

    def test_aws_credentials_denied(self):
        ok, why = master_ai._read_path_ok(str(Path.home() / ".aws" / "credentials"))
        self.assertFalse(ok)
        self.assertIn("aws", why)

    def test_master_ai_keys_denied(self):
        ok, why = master_ai._read_path_ok(str(Path.home() / ".master_ai_keys"))
        self.assertFalse(ok)
        self.assertIn("master_ai_keys", why)

    def test_netrc_denied(self):
        ok, why = master_ai._read_path_ok(str(Path.home() / ".netrc"))
        self.assertFalse(ok)

    def test_etc_shadow_denied(self):
        ok, why = master_ai._read_path_ok("/etc/shadow")
        self.assertFalse(ok)
        self.assertIn("shadow", why)

    def test_etc_sudoers_denied(self):
        ok, why = master_ai._read_path_ok("/etc/sudoers")
        self.assertFalse(ok)

    def test_root_dir_denied(self):
        ok, why = master_ai._read_path_ok("/root/anything")
        self.assertFalse(ok)


class ReadFenceOutsideAllowed(unittest.TestCase):
    def test_etc_passwd_denied(self):
        # /etc isn't in allowed roots — denied even though not in secret list.
        ok, why = master_ai._read_path_ok("/etc/passwd")
        self.assertFalse(ok)
        self.assertIn("outside allowed roots", why)

    def test_proc_denied(self):
        ok, _ = master_ai._read_path_ok("/proc/1/status")
        self.assertFalse(ok)


class ReadFenceSymlinkEscape(unittest.TestCase):
    def setUp(self):
        # Create a symlink inside HOME pointing to /etc/passwd (outside
        # allowed roots and arguably sensitive). The fence should reject
        # the resolved path, not the symlink path.
        self.tmpdir = tempfile.mkdtemp(dir=str(Path.home()))
        self.link = Path(self.tmpdir) / "escape_link"
        try:
            os.symlink("/etc/passwd", self.link)
        except FileExistsError:
            pass

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_symlink_escape_denied(self):
        # The link itself is inside HOME (which is allowed), but the
        # resolved target /etc/passwd is outside. resolve() returns the
        # real target, and the fence checks the real target.
        ok, why = master_ai._read_path_ok(str(self.link))
        self.assertFalse(
            ok, "symlink that resolves outside allowed roots must be denied"
        )


class AgentStandardsFlipped(unittest.TestCase):
    """P2.3 should flip two WARNs to PASS in the standards report."""

    def test_read_path_fence_now_pass(self):
        # Checks are (status, name, detail) tuples.
        checks = master_ai.agent_standards_checks()
        rpf = next((c for c in checks if c[1] == "read path fence"), None)
        self.assertIsNotNone(rpf, "read path fence check missing")
        self.assertEqual(
            rpf[0], "PASS", f"read path fence should be PASS after P2.3: {rpf}"
        )

    def test_output_caps_now_pass(self):
        checks = master_ai.agent_standards_checks()
        oc = next((c for c in checks if c[1] == "output caps"), None)
        self.assertIsNotNone(oc)
        self.assertEqual(oc[0], "PASS", f"output caps should be PASS after P2.3: {oc}")

    def test_score_above_baseline(self):
        score = master_ai.agent_standards_score()
        self.assertGreaterEqual(
            score, 90, f"score should be ≥90 after P2.3 (baseline 87): {score}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
