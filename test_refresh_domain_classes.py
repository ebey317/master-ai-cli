#!/usr/bin/env python3
"""Tests for refresh_domain_classes.sh (Phase 1.5).

Each test invokes the script as a subprocess with isolated env vars
(DOMAIN_CLASSES_OUT, DOMAIN_CLASSES_USER, DOMAIN_CLASSES_SEED) so the
real ~/.master_ai_domain_classes.json is never touched. Tests cover the
merge semantics — seed-only, user override, atomic write — but never
exercise the URLhaus remote pull (DOMAIN_CLASSES_FETCH stays unset).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "refresh_domain_classes.sh"


def _run(env_overrides, expect_exit=0):
    env = os.environ.copy()
    env.update(env_overrides)
    env.setdefault("DOMAIN_CLASSES_FETCH", "0")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if expect_exit is not None:
        if result.returncode != expect_exit:
            raise AssertionError(
                f"refresh exited {result.returncode}, expected {expect_exit}.\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
    return result


class RefreshDomainClassesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="refresh_classes_")
        self.out_path = os.path.join(self.tmp, "out.json")
        self.user_path = os.path.join(self.tmp, "user.json")
        # Use the in-repo seed so the test is deterministic.
        self.seed_path = str(
            Path(__file__).resolve().parent / "master_ai_domain_classes.seed.json"
        )

    def tearDown(self):
        for p in (self.out_path, self.user_path):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _read_output(self):
        with open(self.out_path, encoding="utf-8") as f:
            return json.load(f)

    def test_seed_only(self):
        _run(
            {
                "DOMAIN_CLASSES_OUT": self.out_path,
                "DOMAIN_CLASSES_USER": self.user_path,  # doesn't exist
                "DOMAIN_CLASSES_SEED": self.seed_path,
            }
        )
        data = self._read_output()
        self.assertIn("category_1", data)
        self.assertIn("category_2", data)
        self.assertIn("category_3", data)
        self.assertIn("_meta", data)
        # Seed has one reserved test entry.
        self.assertIn("phishing-example.test", data["category_1"])

    def test_user_additions_merge(self):
        user = {
            "category_1": {"user-bad.example": "user-added category 1"},
            "category_2": {"user-bank.example": "user-added category 2"},
            "category_3": {"user-friction.example": "user-added category 3"},
        }
        with open(self.user_path, "w", encoding="utf-8") as f:
            json.dump(user, f)
        _run(
            {
                "DOMAIN_CLASSES_OUT": self.out_path,
                "DOMAIN_CLASSES_USER": self.user_path,
                "DOMAIN_CLASSES_SEED": self.seed_path,
            }
        )
        data = self._read_output()
        # Seed entry preserved.
        self.assertIn("phishing-example.test", data["category_1"])
        # User additions present in all three buckets.
        self.assertEqual(
            data["category_1"]["user-bad.example"], "user-added category 1"
        )
        self.assertEqual(
            data["category_2"]["user-bank.example"], "user-added category 2"
        )
        self.assertEqual(
            data["category_3"]["user-friction.example"], "user-added category 3"
        )

    def test_user_overrides_seed(self):
        user = {
            "category_1": {"phishing-example.test": "rewritten by user"},
        }
        with open(self.user_path, "w", encoding="utf-8") as f:
            json.dump(user, f)
        _run(
            {
                "DOMAIN_CLASSES_OUT": self.out_path,
                "DOMAIN_CLASSES_USER": self.user_path,
                "DOMAIN_CLASSES_SEED": self.seed_path,
            }
        )
        data = self._read_output()
        self.assertEqual(
            data["category_1"]["phishing-example.test"], "rewritten by user"
        )

    def test_meta_block_populated(self):
        _run(
            {
                "DOMAIN_CLASSES_OUT": self.out_path,
                "DOMAIN_CLASSES_USER": self.user_path,
                "DOMAIN_CLASSES_SEED": self.seed_path,
            }
        )
        data = self._read_output()
        meta = data["_meta"]
        self.assertEqual(meta["source"], "refresh_domain_classes.sh")
        self.assertEqual(meta["version"], 1)
        self.assertGreaterEqual(meta["cat1_count"], 1)
        self.assertIn("updated_iso", meta)
        self.assertIn("urlhaus_added", meta)
        self.assertEqual(meta["urlhaus_added"], 0)  # fetch was disabled

    def test_output_is_valid_classifier_input(self):
        """The output must round-trip through stt_server._load_domain_classes."""
        _run(
            {
                "DOMAIN_CLASSES_OUT": self.out_path,
                "DOMAIN_CLASSES_USER": self.user_path,
                "DOMAIN_CLASSES_SEED": self.seed_path,
            }
        )
        os.environ["SENSEI_TUI"] = "0"
        sys.path.insert(0, os.path.expanduser("~/scripts"))
        import stt_server as srv

        original_path = srv._DOMAIN_CLASSES_PATH
        original_cache = dict(srv._DOMAIN_CLASSES_CACHE)
        try:
            srv._DOMAIN_CLASSES_PATH = self.out_path
            srv._DOMAIN_CLASSES_CACHE["data"] = None
            srv._DOMAIN_CLASSES_CACHE["mtime"] = 0.0
            srv._DOMAIN_CLASSES_CACHE["ts"] = 0.0
            data = srv._load_domain_classes()
            self.assertIn("phishing-example.test", data["category_1"])
            verdict = srv._classify_domain("phishing-example.test")
            self.assertEqual(verdict["category"], 1)
        finally:
            srv._DOMAIN_CLASSES_PATH = original_path
            for k, v in original_cache.items():
                srv._DOMAIN_CLASSES_CACHE[k] = v


if __name__ == "__main__":
    unittest.main()
