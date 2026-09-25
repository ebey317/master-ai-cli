#!/usr/bin/env python3
"""Unit tests for ~/scripts/observability.py (P1.7).

Builds synthetic router_metrics.jsonl + audit_typed.jsonl files in a
temp directory, points summarize() at them, and asserts the rollup
shape. Pure read-side tests — no Sensei or live audit fires.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/scripts"))

import observability as obs  # noqa: E402


class SummaryShape(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.metrics = Path(self.tmp) / "metrics.jsonl"
        self.audit = Path(self.tmp) / "audit.jsonl"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_metrics(self, lines):
        self.metrics.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    def _write_audit(self, lines):
        self.audit.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    def _run(self):
        return obs.summarize(metrics_path=self.metrics, audit_path=self.audit)

    def test_empty_streams_return_zero_counts(self):
        s = self._run()
        self.assertEqual(s["events_scanned"], {"router": 0, "audit": 0})
        self.assertEqual(s["route_decisions"], 0)
        self.assertEqual(s["model_calls"], 0)
        self.assertEqual(s["by_route"], {})
        self.assertEqual(s["by_model"], {})
        self.assertEqual(s["executions"], {"ok": 0, "fail": 0})
        self.assertEqual(s["blocked"]["total"], 0)
        self.assertEqual(s["harvest"], {"hits": 0, "records": 0})

    def test_route_decisions_rolled_up(self):
        self._write_metrics(
            [
                {
                    "kind": "route_decision",
                    "route": "local",
                    "model": "master-ai",
                    "reason": "code",
                },
                {
                    "kind": "route_decision",
                    "route": "local",
                    "model": "master-ai",
                    "reason": "chat",
                },
                {
                    "kind": "route_decision",
                    "route": "cloud_fast",
                    "model": "groq",
                    "reason": "fast",
                },
            ]
        )
        s = self._run()
        self.assertEqual(s["route_decisions"], 3)
        self.assertEqual(s["by_route"]["local"], 2)
        self.assertEqual(s["by_route"]["cloud_fast"], 1)
        self.assertEqual(s["by_model"]["master-ai"], 2)
        self.assertEqual(s["by_model"]["groq"], 1)

    def test_model_calls_counted(self):
        self._write_metrics(
            [
                {"kind": "model_call", "model": "master-ai", "route": "local"},
                {"kind": "model_call", "model": "master-ai", "route": "local"},
                {"kind": "model_call", "model": "groq", "route": "cloud"},
            ]
        )
        s = self._run()
        self.assertEqual(s["model_calls"], 3)
        self.assertEqual(s["by_model"]["master-ai"], 2)

    def test_executions_split_by_ok(self):
        self._write_metrics(
            [
                {"kind": "execution", "action": "run", "ok": True},
                {"kind": "execution", "action": "run", "ok": True},
                {"kind": "execution", "action": "run", "ok": False, "error": "timeout"},
            ]
        )
        s = self._run()
        self.assertEqual(s["executions"], {"ok": 2, "fail": 1})

    def test_audit_status_rolled_up(self):
        self._write_audit(
            [
                {
                    "kind": "RUN",
                    "status": "completed",
                    "risk": "safe",
                    "audit_kind": "RUN",
                },
                {
                    "kind": "RUN",
                    "status": "completed",
                    "risk": "safe",
                    "audit_kind": "RUN",
                },
                {
                    "kind": "EDIT",
                    "status": "blocked",
                    "risk": "normal",
                    "audit_kind": "EDIT-FENCE-BLOCK",
                },
                {
                    "kind": "CREATE",
                    "status": "completed",
                    "risk": "normal",
                    "audit_kind": "CREATE",
                },
            ]
        )
        s = self._run()
        self.assertEqual(s["audit_status"]["completed"], 3)
        self.assertEqual(s["audit_status"]["blocked"], 1)
        self.assertEqual(s["audit_by_kind"]["RUN"], 2)
        self.assertEqual(s["audit_by_kind"]["EDIT"], 1)
        self.assertEqual(s["audit_by_kind"]["CREATE"], 1)
        self.assertEqual(s["audit_by_risk"]["safe"], 2)
        self.assertEqual(s["audit_by_risk"]["normal"], 2)

    def test_blocked_status_flows_into_blocked_rollup(self):
        self._write_audit(
            [
                {
                    "kind": "RUN",
                    "status": "blocked",
                    "risk": "high",
                    "audit_kind": "RUN-BLOCK",
                },
                {
                    "kind": "RUN",
                    "status": "blocked",
                    "risk": "high",
                    "audit_kind": "RUN-BLOCK-CLEANUP",
                },
                {
                    "kind": "EDIT",
                    "status": "blocked",
                    "risk": "normal",
                    "audit_kind": "EDIT-FENCE-BLOCK",
                },
            ]
        )
        s = self._run()
        self.assertEqual(s["blocked"]["total"], 3)
        self.assertEqual(s["blocked"]["by_kind"]["RUN"], 2)
        self.assertEqual(s["blocked"]["by_kind"]["EDIT"], 1)

    def test_hook_block_audit_increments_hook_fires(self):
        self._write_audit(
            [
                {
                    "kind": "EDIT",
                    "status": "blocked",
                    "risk": "normal",
                    "audit_kind": "HOOK-BLOCK-POST_EDIT",
                },
            ]
        )
        s = self._run()
        self.assertEqual(s["hook_fires"], 1)

    def test_fallback_reasons_collected(self):
        self._write_metrics(
            [
                {
                    "kind": "route_decision",
                    "route": "local",
                    "reason": "no cloud keys, fallback to master-ai",
                },
                {
                    "kind": "route_decision",
                    "route": "local",
                    "reason": "qwen3.5:cloud unavailable, using fireworks",
                },
                {
                    "kind": "route_decision",
                    "route": "cloud_fast",
                    "reason": "explicit 'fast:' → Groq",
                },
            ]
        )
        s = self._run()
        # The two fallback/unavailable reasons should appear; the explicit
        # prefix should NOT.
        self.assertEqual(len(s["fallbacks"]), 2)
        for f in s["fallbacks"]:
            r = f.get("reason") or ""
            self.assertTrue("fallback" in r.lower() or "unavailable" in r.lower())

    def test_harvest_metrics_rolled_up(self):
        self._write_metrics(
            [
                {"kind": "harvest_hit", "similarity": 0.9},
                {"kind": "harvest_record", "model": "master-ai"},
                {"kind": "harvest_record", "model": "groq"},
                {"kind": "cached", "similarity": 0.95},
            ]
        )
        s = self._run()
        self.assertEqual(s["harvest"]["hits"], 2)  # harvest_hit + cached
        self.assertEqual(s["harvest"]["records"], 2)

    def test_malformed_lines_skipped(self):
        self.metrics.write_text(
            '{"kind": "route_decision", "route": "local"}\n'
            "not json at all\n"
            '{"kind": "model_call", "model": "groq"}\n'
        )
        s = self._run()
        self.assertEqual(s["events_scanned"]["router"], 2)
        self.assertEqual(s["route_decisions"], 1)
        self.assertEqual(s["model_calls"], 1)

    def test_missing_files_yield_zero(self):
        # Use paths that definitely don't exist.
        s = obs.summarize(
            metrics_path=Path("/tmp/definitely-not-here-xxxxx-metrics.jsonl"),
            audit_path=Path("/tmp/definitely-not-here-xxxxx-audit.jsonl"),
        )
        self.assertEqual(s["events_scanned"], {"router": 0, "audit": 0})


class FormatStats(unittest.TestCase):
    def test_format_returns_string(self):
        summary = obs.summarize(
            metrics_path=Path("/tmp/nope.jsonl"),
            audit_path=Path("/tmp/nope.jsonl"),
        )
        out = obs.format_stats(summary)
        self.assertIsInstance(out, str)
        self.assertIn("Observability", out)
        self.assertIn("Routes", out)
        self.assertIn("Models", out)
        self.assertIn("Blocked actions", out)
        self.assertIn("Hook fires", out)

    def test_format_handles_non_dict(self):
        self.assertEqual(obs.format_stats(None), "(no stats)")
        self.assertEqual(obs.format_stats("string"), "(no stats)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
