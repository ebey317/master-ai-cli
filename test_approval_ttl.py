#!/usr/bin/env python3
"""Unit tests for P2.2 approval TTL + cwd scope.

Verifies the new approved-list contract:
- New approvals carry timestamp + cwd; match only within TTL + matching cwd
- Legacy bare-command lines preserve match-everywhere/no-expiry behavior
- Duplicate writes refresh the timestamp rather than appending forever
- Standards check 'approval expiry' flips to PASS
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


class ApprovedLineParser(unittest.TestCase):
    def test_legacy_bare_line(self):
        # Pre-P2.2 entries: just a command on a line. Parser returns
        # ts=0 + cwd='*' so the matcher treats them as match-everywhere /
        # no-expiry — backward compat.
        r = master_ai._parse_approved_line("ls -la")
        self.assertEqual(r, (0, "*", "ls -la"))

    def test_full_line(self):
        line = "1700000000\t/home/user\tls -la"
        r = master_ai._parse_approved_line(line)
        self.assertEqual(r, (1700000000, "/home/user", "ls -la"))

    def test_empty_returns_none(self):
        self.assertIsNone(master_ai._parse_approved_line(""))
        self.assertIsNone(master_ai._parse_approved_line("   "))

    def test_malformed_returns_none(self):
        # Two tabs but bad timestamp
        r = master_ai._parse_approved_line("not_a_ts\t/cwd\tcmd")
        self.assertIsNone(r)


class IsApprovedTtl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, mode="w")
        self.tmp.close()
        self.path = Path(self.tmp.name)
        self._orig_file = master_ai.APPROVED_FILE
        master_ai.APPROVED_FILE = self.path

    def tearDown(self):
        master_ai.APPROVED_FILE = self._orig_file
        try:
            self.path.unlink()
        except Exception:
            pass

    def test_fresh_match_in_same_cwd(self):
        master_ai.save_approved("ls", cwd="/tmp/scratch", scope="cwd")
        self.assertTrue(master_ai.is_approved("ls", cwd="/tmp/scratch"))

    def test_no_match_in_different_cwd(self):
        master_ai.save_approved("ls", cwd="/tmp/scratch", scope="cwd")
        self.assertFalse(master_ai.is_approved("ls", cwd="/home/elsewhere"))

    def test_global_scope_matches_anywhere(self):
        master_ai.save_approved("uname -a", cwd="/tmp/x", scope="global")
        self.assertTrue(master_ai.is_approved("uname -a", cwd="/tmp/x"))
        self.assertTrue(master_ai.is_approved("uname -a", cwd="/anywhere/else"))

    def test_legacy_bare_line_matches_everywhere(self):
        # Simulate pre-P2.2 file: bare command, no tabs.
        self.path.write_text("legacy-cmd-foo\n")
        self.assertTrue(master_ai.is_approved("legacy-cmd-foo", cwd="/any/dir"))

    def test_expired_entry_does_not_match(self):
        # Write entry with stale timestamp manually.
        old_ts = int(time.time()) - (48 * 3600)  # 48h ago
        self.path.write_text(f"{old_ts}\t/tmp/scratch\told-cmd\n")
        # Default TTL is 24h — 48h-old should not match.
        self.assertFalse(master_ai.is_approved("old-cmd", cwd="/tmp/scratch"))

    def test_save_de_dupes(self):
        master_ai.save_approved("dup", cwd="/tmp/scratch", scope="cwd")
        master_ai.save_approved("dup", cwd="/tmp/scratch", scope="cwd")
        master_ai.save_approved("dup", cwd="/tmp/scratch", scope="cwd")
        # File should contain exactly one entry for "dup" in /tmp/scratch.
        lines = [
            l
            for l in self.path.read_text().splitlines()
            if "dup" in l and "/tmp/scratch" in l
        ]
        self.assertEqual(
            len(lines), 1, f"expected 1 dedup'd entry, got {len(lines)}: {lines}"
        )

    def test_load_approved_returns_command_set(self):
        # Back-compat accessor still works.
        master_ai.save_approved("cmd-a", cwd="/tmp/a", scope="cwd")
        master_ai.save_approved("cmd-b", cwd="/tmp/b", scope="cwd")
        s = master_ai.load_approved()
        self.assertIsInstance(s, set)
        self.assertIn("cmd-a", s)
        self.assertIn("cmd-b", s)


class AgentStandardsApprovalFlipped(unittest.TestCase):
    def test_approval_expiry_now_pass(self):
        checks = master_ai.agent_standards_checks()
        ap = next((c for c in checks if c[1] == "approval expiry"), None)
        self.assertIsNotNone(ap)
        self.assertEqual(
            ap[0], "PASS", f"approval expiry should be PASS after P2.2: {ap}"
        )

    def test_score_in_target_band(self):
        score = master_ai.agent_standards_score()
        self.assertGreaterEqual(
            score, 93, f"score should be ≥93 after P2.3 + P2.2: {score}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
