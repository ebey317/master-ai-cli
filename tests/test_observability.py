import json
import tempfile
import unittest
from pathlib import Path

from observability import _tail_jsonl, format_stats, summarize


class TestTailJsonl(unittest.TestCase):
    def test_missing_path_returns_empty(self):
        result = _tail_jsonl(Path("/nonexistent/file.jsonl"), 100)
        self.assertEqual(result, [])

    def test_none_path_returns_empty(self):
        result = _tail_jsonl(None, 100)
        self.assertEqual(result, [])

    def test_valid_jsonl_parsed(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"a": 1}\n{"b": 2}\n')
            path = Path(f.name)
        try:
            result = _tail_jsonl(path, 100)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["a"], 1)
            self.assertEqual(result[1]["b"], 2)
        finally:
            path.unlink(missing_ok=True)

    def test_malformed_lines_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"ok": 1}\nNOT JSON\n{"ok": 2}\n')
            path = Path(f.name)
        try:
            result = _tail_jsonl(path, 100)
            self.assertEqual(len(result), 2)
        finally:
            path.unlink(missing_ok=True)

    def test_limit_returns_tail(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for i in range(10):
                f.write(f'{{"n": {i}}}\n')
            path = Path(f.name)
        try:
            result = _tail_jsonl(path, 3)
            self.assertEqual(len(result), 3)
            # last 3 of 0..9 → 7, 8, 9
            self.assertEqual(result[0]["n"], 7)
            self.assertEqual(result[2]["n"], 9)
        finally:
            path.unlink(missing_ok=True)

    def test_empty_file_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)
        try:
            result = _tail_jsonl(path, 100)
            self.assertEqual(result, [])
        finally:
            path.unlink(missing_ok=True)

    def test_blank_lines_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('\n\n{"x": 1}\n\n')
            path = Path(f.name)
        try:
            result = _tail_jsonl(path, 100)
            self.assertEqual(len(result), 1)
        finally:
            path.unlink(missing_ok=True)


class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mpath = Path(self.tmpdir.name) / "metrics.jsonl"
        self.apath = Path(self.tmpdir.name) / "audit.jsonl"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_metrics(self, events):
        self.mpath.write_text("\n".join(json.dumps(e) for e in events))

    def _write_audit(self, events):
        self.apath.write_text("\n".join(json.dumps(e) for e in events))

    def _run(self):
        return summarize(metrics_path=self.mpath, audit_path=self.apath)

    def test_missing_files_all_zeros(self):
        result = summarize(
            metrics_path=Path("/nonexistent/m.jsonl"),
            audit_path=Path("/nonexistent/a.jsonl"),
        )
        self.assertEqual(result["route_decisions"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["executions"], {"ok": 0, "fail": 0})
        self.assertEqual(result["blocked"]["total"], 0)
        self.assertEqual(result["harvest"]["hits"], 0)
        self.assertEqual(result["hook_fires"], 0)
        self.assertEqual(result["fallbacks"], [])

    def test_all_required_keys_present_on_empty(self):
        self.mpath.write_text("")
        self.apath.write_text("")
        result = self._run()
        expected = {
            "events_scanned",
            "by_route",
            "by_model",
            "route_decisions",
            "model_calls",
            "executions",
            "blocked",
            "audit_status",
            "audit_by_kind",
            "audit_by_risk",
            "harvest",
            "hook_fires",
            "fallbacks",
        }
        self.assertEqual(set(result.keys()), expected)

    def test_events_scanned_counts(self):
        self._write_metrics([{"kind": "route_decision"}, {"kind": "model_call"}])
        self._write_audit([{"kind": "RUN", "status": "completed"}])
        result = self._run()
        self.assertEqual(result["events_scanned"]["router"], 2)
        self.assertEqual(result["events_scanned"]["audit"], 1)

    def test_route_decision_rollup(self):
        self._write_metrics(
            [
                {
                    "kind": "route_decision",
                    "route": "local",
                    "model": "qwen2",
                    "reason": "default",
                },
                {
                    "kind": "route_decision",
                    "route": "cloud",
                    "model": "claude",
                    "reason": "escalation",
                },
                {
                    "kind": "route_decision",
                    "route": "local",
                    "model": "qwen2",
                    "reason": "default",
                },
            ]
        )
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(result["route_decisions"], 3)
        self.assertEqual(result["by_route"]["local"], 2)
        self.assertEqual(result["by_route"]["cloud"], 1)
        self.assertEqual(result["by_model"]["qwen2"], 2)

    def test_model_call_rollup(self):
        self._write_metrics(
            [
                {"kind": "model_call", "model": "ollama"},
                {"kind": "model_call", "model": "ollama"},
                {"kind": "model_call", "model": "groq"},
            ]
        )
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(result["by_model"]["ollama"], 2)
        self.assertEqual(result["by_model"]["groq"], 1)

    def test_route_decision_model_also_counts_in_by_model(self):
        self._write_metrics(
            [
                {
                    "kind": "route_decision",
                    "route": "local",
                    "model": "qwen2",
                    "reason": "",
                },
            ]
        )
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(result["by_model"]["qwen2"], 1)

    def test_execution_ok_fail_counts(self):
        self._write_metrics(
            [
                {"kind": "execution", "ok": True},
                {"kind": "execution", "ok": True},
                {"kind": "execution", "ok": False},
            ]
        )
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(result["executions"]["ok"], 2)
        self.assertEqual(result["executions"]["fail"], 1)

    def test_harvest_kinds(self):
        self._write_metrics(
            [
                {"kind": "harvest_hit"},
                {"kind": "harvest_lookup"},
                {"kind": "cached"},
                {"kind": "harvest_record"},
            ]
        )
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(result["harvest"]["hits"], 3)
        self.assertEqual(result["harvest"]["records"], 1)

    def test_hook_fires_from_metrics(self):
        self._write_metrics(
            [
                {"kind": "hook_block"},
                {"kind": "HOOK-BLOCK"},
                {"kind": "hook_pre_run"},
            ]
        )
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(result["hook_fires"], 3)

    def test_fallbacks_detected_from_reason(self):
        self._write_metrics(
            [
                {
                    "kind": "route_decision",
                    "route": "cloud",
                    "model": "gpt4",
                    "reason": "local unavailable",
                },
                {
                    "kind": "route_decision",
                    "route": "cloud",
                    "model": "gpt4",
                    "reason": "fallback triggered",
                },
            ]
        )
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(len(result["fallbacks"]), 2)

    def test_fallbacks_capped_at_five(self):
        events = [
            {
                "kind": "route_decision",
                "route": "cloud",
                "model": "g",
                "reason": f"fallback {i}",
            }
            for i in range(10)
        ]
        self._write_metrics(events)
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(len(result["fallbacks"]), 5)

    def test_non_fallback_reason_not_counted(self):
        self._write_metrics(
            [
                {
                    "kind": "route_decision",
                    "route": "local",
                    "model": "q",
                    "reason": "fast local",
                },
            ]
        )
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(result["fallbacks"], [])

    def test_audit_status_rollup(self):
        self._write_audit(
            [
                {"kind": "RUN", "status": "completed", "risk": "normal"},
                {"kind": "RUN", "status": "completed", "risk": "high"},
                {"kind": "EDIT", "status": "blocked", "risk": "normal"},
                {"kind": "READ", "status": "completed", "risk": "safe"},
            ]
        )
        self.mpath.write_text("")
        result = self._run()
        self.assertEqual(result["audit_status"]["completed"], 3)
        self.assertEqual(result["audit_status"]["blocked"], 1)
        self.assertEqual(result["audit_by_kind"]["RUN"], 2)
        self.assertEqual(result["audit_by_kind"]["EDIT"], 1)
        self.assertEqual(result["audit_by_risk"]["normal"], 2)
        self.assertEqual(result["audit_by_risk"]["safe"], 1)

    def test_blocked_audit_records_roll_into_blocked_total(self):
        self._write_audit(
            [
                {"kind": "RUN", "status": "blocked", "risk": "high"},
                {"kind": "EDIT", "status": "blocked", "risk": "normal"},
            ]
        )
        self.mpath.write_text("")
        result = self._run()
        self.assertEqual(result["blocked"]["total"], 2)
        self.assertEqual(result["blocked"]["by_kind"]["RUN"], 1)
        self.assertEqual(result["blocked"]["by_kind"]["EDIT"], 1)

    def test_blocked_audit_with_no_kind_uses_unknown(self):
        self._write_audit([{"status": "blocked"}])
        self.mpath.write_text("")
        result = self._run()
        self.assertEqual(result["blocked"]["total"], 1)
        self.assertIn("unknown", result["blocked"]["by_kind"])

    def test_hook_fires_from_audit_kind(self):
        self._write_audit([{"audit_kind": "HOOK-BLOCK-run", "status": "completed"}])
        self.mpath.write_text("")
        result = self._run()
        self.assertEqual(result["hook_fires"], 1)

    def test_unknown_router_kind_ignored(self):
        self._write_metrics([{"kind": "unknown_kind", "data": "x"}])
        self.apath.write_text("")
        result = self._run()
        self.assertEqual(result["route_decisions"], 0)
        self.assertEqual(result["model_calls"], 0)

    def test_limit_parameter_respected(self):
        events = [{"kind": "model_call", "model": f"m{i}"} for i in range(20)]
        self._write_metrics(events)
        self.apath.write_text("")
        result = summarize(limit=5, metrics_path=self.mpath, audit_path=self.apath)
        self.assertEqual(result["model_calls"], 5)
        self.assertEqual(result["events_scanned"]["router"], 5)


class TestFormatStats(unittest.TestCase):
    def _base_summary(self, **overrides):
        base = {
            "events_scanned": {"router": 0, "audit": 0},
            "by_route": {},
            "by_model": {},
            "route_decisions": 0,
            "model_calls": 0,
            "executions": {"ok": 0, "fail": 0},
            "blocked": {"total": 0, "by_kind": {}},
            "audit_status": {},
            "audit_by_kind": {},
            "audit_by_risk": {},
            "harvest": {"hits": 0, "records": 0},
            "hook_fires": 0,
            "fallbacks": [],
        }
        base.update(overrides)
        return base

    def test_non_dict_returns_no_stats_string(self):
        self.assertEqual(format_stats(None), "(no stats)")
        self.assertEqual(format_stats("oops"), "(no stats)")
        self.assertEqual(format_stats(42), "(no stats)")

    def test_empty_dict_renders_without_crash(self):
        result = format_stats({})
        self.assertIsInstance(result, str)
        self.assertIn("scanned", result)

    def test_full_summary_contains_expected_sections(self):
        summary = self._base_summary(
            events_scanned={"router": 100, "audit": 50},
            by_route={"local": 80, "cloud": 20},
            by_model={"qwen2": 70, "claude": 30},
        )
        result = format_stats(summary)
        self.assertIn("100", result)
        self.assertIn("local", result)
        self.assertIn("Routes", result)
        self.assertIn("Models", result)
        self.assertIn("Harvest", result)
        self.assertIn("Hook fires", result)

    def test_success_rate_calculation(self):
        summary = self._base_summary(executions={"ok": 3, "fail": 1})
        result = format_stats(summary)
        self.assertIn("75%", result)

    def test_zero_executions_no_div_zero(self):
        summary = self._base_summary(executions={"ok": 0, "fail": 0})
        result = format_stats(summary)
        self.assertIn("0%", result)

    def test_fallbacks_section_present_when_non_empty(self):
        summary = self._base_summary(
            fallbacks=[{"reason": "local unavailable", "ts": "2026-01-01T00:00:00"}]
        )
        result = format_stats(summary)
        self.assertIn("fallback", result.lower())
        self.assertIn("local unavailable", result)

    def test_fallbacks_section_absent_when_empty(self):
        summary = self._base_summary(fallbacks=[])
        result = format_stats(summary)
        self.assertNotIn("Recent fallbacks", result)

    def test_blocked_total_rendered(self):
        summary = self._base_summary(
            blocked={"total": 7, "by_kind": {"RUN": 5, "EDIT": 2}}
        )
        result = format_stats(summary)
        self.assertIn("7", result)
        self.assertIn("RUN", result)

    def test_returns_string(self):
        summary = self._base_summary()
        self.assertIsInstance(format_stats(summary), str)


if __name__ == "__main__":
    unittest.main()
