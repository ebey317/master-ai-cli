#!/usr/bin/env python3
"""End-to-end tests for the RUN sandbox boundary (Phase 1.2).

These run real subprocesses through the real sandbox wrapper
(_build_sandbox_argv / run_command) -- that's the point, this verifies
actual containment on this machine, not a mock of the intent. No test
here prints or asserts on real secret file *content* -- only size/entry
counts, so a passing run never actually exposes what's in ~/.ssh or
~/.master_ai_keys, matching the same restraint used to verify this
manually before writing these tests.

Run: python3 ~/scripts/test_sandbox_escape.py
Exit: 0 = all green, non-zero = a real containment failure.
"""
import os
import sys
import time
import unittest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402


def _proc_count_for_user():
    import subprocess
    out = subprocess.run(["ps", "-u", os.environ.get("USER", "elijah"), "--no-headers"],
                         capture_output=True, text=True).stdout
    return len(out.splitlines())


class NormalCommandsStillWork(unittest.TestCase):
    def test_echo(self):
        r = master_ai.run_command("echo hi")
        self.assertTrue(r.ok)
        self.assertEqual(r.strip(), "hi")

    def test_network_access_preserved(self):
        # Regression test for the -n mistake: the roadmap's literal wrapper
        # put RUN in a fresh network namespace with zero connectivity.
        # Real daily use (per ~/.master_ai_audit.log) leans on curl/wget
        # constantly -- this must keep working.
        r = master_ai.run_command("curl -s --max-time 8 https://api.github.com/zen")
        self.assertTrue(r.ok, f"network access broke under the sandbox: {r!r}")
        self.assertTrue(len(r.strip()) > 0)

    def test_normal_home_access_outside_hidden_paths(self):
        r = master_ai.run_command("ls ~/scripts | head -3")
        self.assertTrue(r.ok)
        self.assertTrue(len(r.strip()) > 0)

    def test_sh_script_target_still_works_under_wrapper(self):
        script = os.path.expanduser("~/.master_ai_sandbox_test_script.sh")
        with open(script, "w") as f:
            f.write("#!/bin/bash\necho script-ran-ok\n")
        os.chmod(script, 0o755)
        try:
            r = master_ai.run_command(script)
            self.assertTrue(r.ok)
            self.assertIn("script-ran-ok", r)
        finally:
            os.remove(script)


class SecretPathsHidden(unittest.TestCase):
    def test_master_ai_keys_unreadable_from_inside(self):
        real = os.path.realpath(os.path.expanduser("~/.master_ai_keys"))
        if not (os.path.isfile(real) and os.path.getsize(real) > 0):
            self.skipTest("no real ~/.master_ai_keys on this box to test against")
        r = master_ai.run_command("wc -c < ~/.master_ai_keys 2>/dev/null || echo 0")
        size_seen = int(r.strip().split()[0]) if r.strip().split() else -1
        self.assertEqual(size_seen, 0, "sandbox failed to hide ~/.master_ai_keys contents")

    def test_ssh_dir_contents_hidden_from_inside(self):
        real = os.path.expanduser("~/.ssh")
        if not os.path.isdir(real) or not os.listdir(real):
            self.skipTest("no populated ~/.ssh on this box to test against")
        outside_count = len(os.listdir(real))
        r = master_ai.run_command("ls -a ~/.ssh 2>&1 | wc -l")
        inside_count = int(r.strip().split()[0]) if r.strip().split() else -1
        # Empty tmpfs overlay shows only "." and ".." -> 2 lines.
        self.assertEqual(inside_count, 2,
                         f"expected ~/.ssh to read as empty inside the sandbox, "
                         f"got {inside_count} entries (outside has {outside_count})")


class ForkBombContained(unittest.TestCase):
    def test_fork_bomb_does_not_run_away_system_wide(self):
        before = _proc_count_for_user()
        # run_command's own 300s timeout is the outer bound; the cgroup
        # TasksMax is what actually contains this quickly. Give it a few
        # seconds to plateau, then check the system-wide count never
        # climbed anywhere near an unbounded run.
        import threading
        result_holder = {}

        def _run():
            result_holder["r"] = master_ai.run_command(
                "timeout 4 bash -c ':(){ :|:& };:'"
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(3)
        during = _proc_count_for_user()
        t.join(timeout=10)
        # Contained means "didn't run away" -- a generous headroom check,
        # not an exact number (cgroup TasksMax=200 for this scope, but
        # other unrelated processes exist on a live desktop too).
        self.assertLess(during, before + 250,
                        f"fork bomb grew system-wide process count from "
                        f"{before} to {during} -- containment failed")


class StandardsCheckReflectsSandbox(unittest.TestCase):
    def test_sandbox_boundary_check_passes_on_live_probe(self):
        checks = master_ai.agent_standards_checks()
        row = next(c for c in checks if c[1] == "sandbox boundary")
        self.assertEqual(row[0], "PASS")


if __name__ == "__main__":
    unittest.main()
