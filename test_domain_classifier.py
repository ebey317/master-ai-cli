#!/usr/bin/env python3
"""Unit tests for the Phase 1.1 domain classifier in stt_server.

Exercises the pure helpers (_extract_host, _domain_matches, _classify_domain,
_load_domain_classes) against an in-memory class dict so the tests don't
depend on the on-disk ~/.master_ai_domain_classes.json. The HTTP handler is
covered separately by integration tests.
"""

import json
import os
import sys
import tempfile
import time
import unittest

import pytest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

srv = pytest.importorskip("stt_server")

_CLASSES_FIXTURE = {
    "category_1": {
        "phishing-example.test": "Reserved phishing test domain.",
        "bad-actor.example": "Known credential harvester.",
    },
    "category_2": {
        "examplebank.com": "Banking auth surface.",
    },
    "category_3": {
        "adult-example.com": "Adult content — force confirm.",
    },
    "_meta": {"version": 1, "source": "fixture"},
}


class ExtractHostTests(unittest.TestCase):
    def test_bare_domain(self):
        self.assertEqual(srv._extract_host("Example.COM"), "example.com")

    def test_url_with_scheme(self):
        self.assertEqual(
            srv._extract_host("https://example.com/path?x=1"), "example.com"
        )

    def test_url_with_port(self):
        self.assertEqual(srv._extract_host("http://example.com:8080/x"), "example.com")

    def test_url_with_userinfo(self):
        self.assertEqual(
            srv._extract_host("https://user:pw@example.com/a"), "example.com"
        )

    def test_bare_domain_with_port(self):
        self.assertEqual(srv._extract_host("example.com:8080"), "example.com")

    def test_bare_domain_with_path(self):
        self.assertEqual(srv._extract_host("example.com/foo"), "example.com")

    def test_empty_returns_empty(self):
        self.assertEqual(srv._extract_host(""), "")
        self.assertEqual(srv._extract_host(None), "")

    def test_strips_trailing_dot(self):
        self.assertEqual(srv._extract_host("example.com."), "example.com")


class DomainMatchesTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(srv._domain_matches("foo.com", "foo.com"))

    def test_subdomain_match(self):
        self.assertTrue(srv._domain_matches("www.foo.com", "foo.com"))
        self.assertTrue(srv._domain_matches("login.www.foo.com", "foo.com"))

    def test_substring_does_not_match(self):
        # 'badfoo.com' must NOT match the entry 'foo.com'.
        self.assertFalse(srv._domain_matches("badfoo.com", "foo.com"))

    def test_unrelated_does_not_match(self):
        self.assertFalse(srv._domain_matches("bar.com", "foo.com"))

    def test_empty_inputs(self):
        self.assertFalse(srv._domain_matches("", "foo.com"))
        self.assertFalse(srv._domain_matches("foo.com", ""))

    def test_case_insensitive(self):
        self.assertTrue(srv._domain_matches("FOO.COM", "foo.com"))
        self.assertTrue(srv._domain_matches("foo.com", "FOO.COM"))


class ClassifyDomainTests(unittest.TestCase):
    def test_unknown_returns_category_0(self):
        result = srv._classify_domain("ordinary-news.example", classes=_CLASSES_FIXTURE)
        self.assertEqual(result["category"], 0)
        self.assertEqual(result["host"], "ordinary-news.example")
        self.assertEqual(result["matched"], "")
        self.assertEqual(result["source"], "default")

    def test_category_1_hit(self):
        result = srv._classify_domain("phishing-example.test", classes=_CLASSES_FIXTURE)
        self.assertEqual(result["category"], 1)
        self.assertEqual(result["matched"], "phishing-example.test")
        self.assertIn("test", result["reason"].lower())

    def test_category_1_subdomain(self):
        result = srv._classify_domain(
            "login.phishing-example.test", classes=_CLASSES_FIXTURE
        )
        self.assertEqual(result["category"], 1)
        self.assertEqual(result["matched"], "phishing-example.test")

    def test_category_2_hit(self):
        result = srv._classify_domain(
            "https://examplebank.com/login", classes=_CLASSES_FIXTURE
        )
        self.assertEqual(result["category"], 2)
        self.assertEqual(result["host"], "examplebank.com")

    def test_category_3_hit(self):
        result = srv._classify_domain("adult-example.com", classes=_CLASSES_FIXTURE)
        self.assertEqual(result["category"], 3)

    def test_strictest_wins(self):
        # If a host could match both category 2 and category 3, category 1/2
        # win because we iterate (1, 2, 3) and take the first hit.
        classes = {
            "category_1": {},
            "category_2": {"sample.test": "in cat 2"},
            "category_3": {"sample.test": "in cat 3"},
            "_meta": {},
        }
        result = srv._classify_domain("sample.test", classes=classes)
        self.assertEqual(result["category"], 2)

    def test_empty_input_is_safe_default(self):
        result = srv._classify_domain("", classes=_CLASSES_FIXTURE)
        self.assertEqual(result["category"], 0)
        self.assertEqual(result["host"], "")
        self.assertIn("no host", result["reason"])

    def test_ttl_present_and_positive(self):
        result = srv._classify_domain("phishing-example.test", classes=_CLASSES_FIXTURE)
        self.assertIsInstance(result["ttl_s"], int)
        self.assertGreater(result["ttl_s"], 0)


class LoadDomainClassesTests(unittest.TestCase):
    def setUp(self):
        self._orig_path = srv._DOMAIN_CLASSES_PATH
        self._orig_cache = dict(srv._DOMAIN_CLASSES_CACHE)
        self._tmp = tempfile.NamedTemporaryFile(
            prefix="domain_classes_",
            suffix=".json",
            delete=False,
            mode="w",
            encoding="utf-8",
        )
        json.dump(_CLASSES_FIXTURE, self._tmp)
        self._tmp.flush()
        self._tmp.close()
        srv._DOMAIN_CLASSES_PATH = self._tmp.name
        # Clear the cache so the test path is picked up.
        srv._DOMAIN_CLASSES_CACHE["data"] = None
        srv._DOMAIN_CLASSES_CACHE["mtime"] = 0.0
        srv._DOMAIN_CLASSES_CACHE["ts"] = 0.0

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass
        srv._DOMAIN_CLASSES_PATH = self._orig_path
        for k, v in self._orig_cache.items():
            srv._DOMAIN_CLASSES_CACHE[k] = v

    def test_loads_buckets(self):
        data = srv._load_domain_classes()
        self.assertIn("phishing-example.test", data["category_1"])
        self.assertIn("examplebank.com", data["category_2"])
        self.assertIn("adult-example.com", data["category_3"])

    def test_missing_file_returns_empty_shape(self):
        # Point at a non-existent path; loader should fail-open with empty buckets.
        srv._DOMAIN_CLASSES_PATH = (
            "/tmp/__definitely_missing_master_ai_domain_classes.json"
        )
        srv._DOMAIN_CLASSES_CACHE["data"] = None
        srv._DOMAIN_CLASSES_CACHE["mtime"] = 0.0
        srv._DOMAIN_CLASSES_CACHE["ts"] = 0.0
        data = srv._load_domain_classes()
        self.assertEqual(data["category_1"], {})
        self.assertEqual(data["category_2"], {})
        self.assertEqual(data["category_3"], {})

    def test_unreadable_json_returns_empty_shape(self):
        # Write garbage to the file and confirm the loader still returns empties.
        with open(self._tmp.name, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        # Touch mtime so cache invalidates.
        new_mtime = time.time() + 10
        os.utime(self._tmp.name, (new_mtime, new_mtime))
        srv._DOMAIN_CLASSES_CACHE["data"] = None
        srv._DOMAIN_CLASSES_CACHE["mtime"] = 0.0
        srv._DOMAIN_CLASSES_CACHE["ts"] = 0.0
        data = srv._load_domain_classes()
        self.assertEqual(data["category_1"], {})

    def test_mtime_reload(self):
        # First read.
        data = srv._load_domain_classes()
        self.assertIn("examplebank.com", data["category_2"])
        # Rewrite the file with a different entry and bump mtime.
        new_payload = {
            "category_1": {"different.test": "swapped"},
            "category_2": {},
            "category_3": {},
            "_meta": {},
        }
        with open(self._tmp.name, "w", encoding="utf-8") as f:
            json.dump(new_payload, f)
        new_mtime = time.time() + 100
        os.utime(self._tmp.name, (new_mtime, new_mtime))
        # Loader should pick up the new content because mtime changed.
        data2 = srv._load_domain_classes()
        self.assertIn("different.test", data2["category_1"])
        self.assertNotIn("examplebank.com", data2["category_2"])


if __name__ == "__main__":
    unittest.main()
