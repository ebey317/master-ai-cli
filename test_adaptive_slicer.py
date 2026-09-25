#!/usr/bin/env python3
"""Unit tests for the P1.1 adaptive slicer.

Verifies that _adaptive_slice_params() scales pre/post/max_chars by
(1) symbol reference density in the file content and
(2) intent verbs in the user prompt.
Also pins that _slice_around_symbol now accepts max_chars as a parameter
so density-tuned caps actually take effect at truncate time.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.expanduser("~/scripts"))

import master_ai  # noqa: E402

# Synthetic file contents at different densities. Keep them small so the
# slicer can find the symbol's def line and the truncation cap is the
# obvious failure mode if pre/post/max_chars aren't wired through.
_SPARSE_CONTENT = "\n".join(
    [
        "# only one ref to FOO in this file",
        "FOO = 'sparse'",
        "x = 1",
        "y = 2",
    ]
    + ["pad = 'x'"] * 50
)

_DENSE_CONTENT = "\n".join(
    [f"line_{i} = FOO + {i}" for i in range(50)]
    + ["def FOO():", "    pass"]
    + [f"call_FOO_{i}()" for i in range(30)]
)

_DEFAULT_CONTENT = "\n".join(
    ["pad"] * 20
    + ["def kFn():", "    return 1"]
    + [f"result = kFn() + {i}" for i in range(7)]
    + ["pad"] * 30
)


class AdaptiveParamsDensity(unittest.TestCase):
    def test_sparse_returns_tight(self):
        pre, post, mc = master_ai._adaptive_slice_params(
            _SPARSE_CONTENT, "FOO", "explain FOO"
        )
        self.assertEqual((pre, post, mc), (30, 60, 5000))

    def test_default_density_returns_baseline(self):
        pre, post, mc = master_ai._adaptive_slice_params(
            _DEFAULT_CONTENT, "kFn", "explain kFn"
        )
        self.assertEqual(pre, master_ai._SLICER_PRE_LINES)
        self.assertEqual(post, master_ai._SLICER_POST_LINES)
        self.assertEqual(mc, master_ai._SLICER_MAX_CHARS)

    def test_dense_returns_expanded(self):
        pre, post, mc = master_ai._adaptive_slice_params(
            _DENSE_CONTENT, "FOO", "explain FOO"
        )
        self.assertEqual((pre, post, mc), (80, 150, 12000))


class AdaptiveParamsIntent(unittest.TestCase):
    def test_tighter_intent_shrinks_baseline(self):
        pre, post, mc = master_ai._adaptive_slice_params(
            _DEFAULT_CONTENT, "kFn", "where is kFn"
        )
        self.assertLess(pre, master_ai._SLICER_PRE_LINES)
        self.assertLess(post, master_ai._SLICER_POST_LINES)
        self.assertLess(mc, master_ai._SLICER_MAX_CHARS)
        # And never below the floor
        self.assertGreaterEqual(pre, 20)
        self.assertGreaterEqual(post, 40)
        self.assertGreaterEqual(mc, 4000)

    def test_wider_intent_expands_baseline(self):
        pre, post, mc = master_ai._adaptive_slice_params(
            _DEFAULT_CONTENT, "kFn", "debug why kFn is failing"
        )
        self.assertGreater(pre, master_ai._SLICER_PRE_LINES)
        self.assertGreater(post, master_ai._SLICER_POST_LINES)
        self.assertGreater(mc, master_ai._SLICER_MAX_CHARS)

    def test_wider_intent_on_dense_still_expands_further(self):
        # Dense baseline is already (80, 150, 12000). A wider intent should
        # push that higher.
        pre, post, mc = master_ai._adaptive_slice_params(
            _DENSE_CONTENT, "FOO", "debug why FOO regressed"
        )
        self.assertGreater(pre, 80)
        self.assertGreater(post, 150)
        self.assertGreater(mc, 12000)

    def test_tighter_intent_on_sparse_stays_at_floor(self):
        # Sparse baseline (30, 60, 5000) ×0.6 = (18, 36, 3500) but floors at
        # (20, 40, 4000).
        pre, post, mc = master_ai._adaptive_slice_params(
            _SPARSE_CONTENT, "FOO", "where is FOO"
        )
        self.assertGreaterEqual(pre, 20)
        self.assertGreaterEqual(post, 40)
        self.assertGreaterEqual(mc, 4000)


class AdaptiveParamsGuards(unittest.TestCase):
    def test_empty_content_returns_defaults(self):
        pre, post, mc = master_ai._adaptive_slice_params("", "FOO", "explain FOO")
        self.assertEqual(
            (pre, post, mc),
            (
                master_ai._SLICER_PRE_LINES,
                master_ai._SLICER_POST_LINES,
                master_ai._SLICER_MAX_CHARS,
            ),
        )

    def test_empty_symbol_returns_defaults(self):
        pre, post, mc = master_ai._adaptive_slice_params(_DENSE_CONTENT, "", "explain")
        self.assertEqual(
            (pre, post, mc),
            (
                master_ai._SLICER_PRE_LINES,
                master_ai._SLICER_POST_LINES,
                master_ai._SLICER_MAX_CHARS,
            ),
        )

    def test_no_intent_match_uses_density_only(self):
        # "show me the value" matches neither tighter nor wider regex →
        # density baseline returned unmodified.
        pre, post, mc = master_ai._adaptive_slice_params(
            _DEFAULT_CONTENT, "kFn", "show me the value"
        )
        self.assertEqual(pre, master_ai._SLICER_PRE_LINES)
        self.assertEqual(post, master_ai._SLICER_POST_LINES)
        self.assertEqual(mc, master_ai._SLICER_MAX_CHARS)


class SliceAroundSymbolMaxChars(unittest.TestCase):
    """The slicer must honor an explicit max_chars override, since the
    adaptive helper computes a per-call cap that needs to actually fire
    at truncation time. This pins the parameter wiring."""

    def test_max_chars_override_truncates(self):
        # Long synthetic content so truncation must trigger at low caps.
        content = "\n".join(
            ["pad"] * 100
            + ["def target_fn():"]
            + ["    body line " + str(i) for i in range(500)]
        )
        result = master_ai._slice_around_symbol(
            content,
            "target_fn",
            pre_lines=200,
            post_lines=200,
            max_chars=500,
        )
        self.assertIsNotNone(result)
        _, _, slice_text, _ = result
        self.assertTrue(
            slice_text.endswith("chars] ..."),
            f"slicer did not truncate at max_chars=500; len={len(slice_text)}",
        )
        self.assertIn("[TRUNCATED at 500 chars]", slice_text)

    def test_default_max_chars_preserves_legacy_behavior(self):
        # No max_chars override → uses _SLICER_MAX_CHARS = 8000 default.
        content = "\n".join(["pad"] * 50 + ["def Foo():", "    return 1"])
        result = master_ai._slice_around_symbol(content, "Foo")
        self.assertIsNotNone(result)
        _, _, slice_text, _ = result
        # Short content; should not truncate at all.
        self.assertNotIn("[TRUNCATED", slice_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
