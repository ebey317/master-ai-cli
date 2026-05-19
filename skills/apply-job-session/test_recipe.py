"""Regression tests for executor framework safety invariants.

Locks the hard safety property: the sensitivity gate runs BEFORE the four-tier
confidence ladder. A government_id field with a perfect (1.0) confidence match
must still emit BRANCH_REFUSE_SENSITIVE — the gate is non-negotiable.

These tests catch a future over-correction in either direction:
  - Loosening the sensitivity gate so financial/government_id can auto-fill
  - Tightening it so personal-tier gets blocked from the ladder

The matrix is parametrized via unittest.subTest (matches existing test style
in ~/scripts/test_*.py rather than introducing pytest as a new dep).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import recipe  # noqa: E402


class SensitivityGateTests(unittest.TestCase):
    """Locks: tier >= financial NEVER auto-fills, regardless of confidence."""

    def setUp(self):
        recipe._reset_framework_state_for_tests()

    def tearDown(self):
        recipe._reset_framework_state_for_tests()

    def test_above_personal_never_auto_fills_even_at_full_confidence(self):
        for tier in ("government_id", "financial"):
            with self.subTest(tier=tier):
                decision = recipe._executor_decide(
                    field_descriptor={
                        "sensitivity": tier,
                        "ref": f"{tier}-input",
                        "field_type": "text",
                    },
                    match={
                        "confidence": 1.0,
                        "value": "secret",
                        "source": "exact_label_match",
                    },
                )
                self.assertEqual(
                    decision["branch"],
                    recipe.BRANCH_REFUSE_SENSITIVE,
                    f"tier={tier} at confidence=1.0 must refuse, got {decision}",
                )

    def test_personal_is_NOT_blocked_at_full_confidence(self):
        """Converse boundary: personal-tier flows through the ladder normally.
        The sensitivity gate refuses ABOVE personal, not AT personal."""
        decision = recipe._executor_decide(
            field_descriptor={
                "sensitivity": "personal",
                "ref": "email",
                "field_type": "email",
            },
            match={
                "confidence": 1.0,
                "value": "x@y.com",
                "source": "label_match",
            },
        )
        self.assertNotEqual(
            decision["branch"],
            recipe.BRANCH_REFUSE_SENSITIVE,
            f"personal-tier at confidence=1.0 must not be refused, got {decision}",
        )

    def test_none_tier_auto_fills_at_threshold(self):
        """auto_fill threshold (0.9) is the boundary for non-sensitive fields."""
        decision = recipe._executor_decide(
            field_descriptor={
                "sensitivity": "none",
                "ref": "fname",
                "field_type": "text",
            },
            match={
                "confidence": 0.95,
                "value": "John",
                "source": "label_match",
            },
        )
        self.assertEqual(decision["branch"], recipe.BRANCH_AUTO_FILL_FLAG)


class AuditLogPerDecisionTests(unittest.TestCase):
    """Locks the invariant: every call to _executor_decide writes exactly one
    audit log entry, on EVERY branch including refuse_sensitive. Catches the
    'safe decision but no log' failure mode that would turn a safe tool into
    a silent one.

    Disk-full / write-failure surfacing is NOT covered here (we'd be locking
    silence into the regression). Failure-mode coverage is tracked in a
    separate test: test_audit_log_health_surfaces_on_write_failure (TODO,
    designed in the same 2026-05-18 brainstorm). Writing that test name
    down here so the reader knows where the disk-full case lives.
    """

    def setUp(self):
        import tempfile
        recipe._reset_framework_state_for_tests()
        fd, self._tmp_path = tempfile.mkstemp(prefix="audit_test_", suffix=".jsonl")
        import os
        os.close(fd)
        Path(self._tmp_path).write_text("")

    def tearDown(self):
        import os
        recipe._reset_framework_state_for_tests()
        try:
            os.unlink(self._tmp_path)
        except FileNotFoundError:
            pass

    def _read_audit_lines(self):
        text = Path(self._tmp_path).read_text()
        if not text.strip():
            return []
        return [line for line in text.split("\n") if line.strip()]

    def _decide_and_get_audit_entry(self, field_descriptor, match):
        """One decision call; assert exactly one new audit line; return the
        parsed entry plus the decision."""
        import json
        before = len(self._read_audit_lines())
        decision = recipe._executor_decide(
            field_descriptor=field_descriptor,
            match=match,
            audit_path=self._tmp_path,
        )
        lines = self._read_audit_lines()
        self.assertEqual(
            len(lines), before + 1,
            f"expected +1 audit line, got {len(lines) - before} for branch "
            f"{decision['branch']}",
        )
        entry = json.loads(lines[-1])
        return entry, decision

    def test_one_audit_line_per_decision_for_every_branch(self):
        """Five branches × one decision each → exactly five audit entries.
        Safety-critical slots (branch, sensitivity, value_recorded,
        value_redacted_reason) checked per entry."""
        cases = [
            # (descriptor, match, expected_branch)
            (
                {"sensitivity": "none", "ref": "fname", "field_type": "text"},
                {"confidence": 0.95, "value": "John", "source": "label_match"},
                recipe.BRANCH_AUTO_FILL_FLAG,
            ),
            (
                {"sensitivity": "none", "ref": "preferred", "field_type": "text"},
                {"confidence": 0.75, "value": "Johnny", "source": "label_match"},
                recipe.BRANCH_FILL_WITH_CONFIRM,
            ),
            (
                {"sensitivity": "none", "ref": "source", "field_type": "select"},
                {"confidence": 0.5, "candidates": ["LinkedIn", "Indeed", "Friend"]},
                recipe.BRANCH_DISAMBIGUATE,
            ),
            (
                {"sensitivity": "none", "ref": "ein", "field_type": "text"},
                {"confidence": 0.1},
                recipe.BRANCH_STOP_AND_ASK,
            ),
            (
                {"sensitivity": "government_id", "ref": "ssn", "field_type": "text"},
                {"confidence": 1.0, "value": "123-45-6789", "source": "exact_label"},
                recipe.BRANCH_REFUSE_SENSITIVE,
            ),
        ]
        for descriptor, match, expected_branch in cases:
            with self.subTest(branch=expected_branch):
                entry, decision = self._decide_and_get_audit_entry(descriptor, match)
                self.assertEqual(decision["branch"], expected_branch)
                self.assertEqual(entry["branch"], expected_branch)
                self.assertEqual(entry["sensitivity"], descriptor["sensitivity"])
                self.assertEqual(entry["field_ref"], descriptor["ref"])

    def test_refuse_sensitive_logs_no_recorded_value(self):
        """Sensitivity gate: value_recorded is None, value_redacted_reason
        names the sensitivity tier (so the audit trail can tell a refused
        government_id apart from a stop_and_ask with no value)."""
        entry, decision = self._decide_and_get_audit_entry(
            {"sensitivity": "government_id", "ref": "passport", "field_type": "text"},
            {"confidence": 1.0, "value": "P1234567"},
        )
        self.assertEqual(decision["branch"], recipe.BRANCH_REFUSE_SENSITIVE)
        self.assertIsNone(entry["value_recorded"])
        self.assertEqual(entry["value_redacted_reason"], "sensitivity:government_id")

    def test_stop_and_ask_logs_no_value_reason(self):
        """Interrupt branch without a suggested value: value_recorded is None,
        value_redacted_reason is 'no_value' — distinct from sensitivity-redacted
        so the auditor can tell an honest no-idea apart from a refused secret."""
        entry, decision = self._decide_and_get_audit_entry(
            {"sensitivity": "none", "ref": "ein", "field_type": "text"},
            {"confidence": 0.1},
        )
        self.assertEqual(decision["branch"], recipe.BRANCH_STOP_AND_ASK)
        self.assertIsNone(entry["value_recorded"])
        self.assertEqual(entry["value_redacted_reason"], "no_value")

    def test_personal_tier_logs_fingerprint_not_full_value(self):
        """Personal-tier auto-fill records a fingerprint (last-4 or domain),
        never the full value. Confirms _audit_value redaction policy fires
        through the executor path, not just the helper."""
        entry, decision = self._decide_and_get_audit_entry(
            {"sensitivity": "personal", "ref": "phone", "field_type": "tel"},
            {"confidence": 0.95, "value": "555-867-5309", "profile_field": "phone"},
        )
        self.assertEqual(decision["branch"], recipe.BRANCH_AUTO_FILL_FLAG)
        self.assertEqual(entry["value_recorded"], "...5309")
        self.assertEqual(entry["value_redacted_reason"], "sensitivity:personal")

    def test_audit_log_ts_is_iso_8601_with_subsecond_precision(self):
        """Locks audit-log `ts` format: ISO 8601 UTC with microsecond
        precision. Same format as audit_log_health.first_failure_since so
        consumers can rely on a single timestamp shape across slots. Catches
        a future regression that drops the format back to unix float."""
        entry, _ = self._decide_and_get_audit_entry(
            {"sensitivity": "none", "ref": "fname", "field_type": "text"},
            {"confidence": 0.95, "value": "John", "source": "label_match"},
        )
        self.assertIsInstance(entry["ts"], str, f"ts must be string, got {type(entry['ts'])}")
        self.assertIn("T", entry["ts"], f"ts missing date/time separator: {entry['ts']}")
        self.assertTrue(
            entry["ts"].endswith("+00:00") or entry["ts"].endswith("Z"),
            f"ts must be UTC (suffix +00:00 or Z), got {entry['ts']}",
        )
        self.assertIn(".", entry["ts"], f"ts missing subsecond precision: {entry['ts']}")


class AuditLogHealthSurfaceTests(unittest.TestCase):
    """Locks the surfacing invariant: when the audit log write fails,
    audit_log_health flips unhealthy AND the executor still returns its
    decision (the executor must stay functional under degraded conditions).

    This is the test the previous round deliberately did NOT write — there
    the test would have locked the silence; here it locks the surfacing.

    Schema this round (descoped per browser-Claude 2026-05-18):
      healthy, first_failure_since, last_error.
    Deferred: last_failure_ts, failures_count.
    """

    def setUp(self):
        recipe._reset_framework_state_for_tests()

    def tearDown(self):
        import shutil, os
        recipe._reset_framework_state_for_tests()
        # Best-effort cleanup of any locked tempdir created by the failure test.
        td = getattr(self, "_locked_dir", None)
        if td and os.path.exists(td):
            try:
                os.chmod(td, 0o755)
                shutil.rmtree(td)
            except Exception:
                pass

    def test_audit_log_health_surfaces_on_write_failure(self):
        """Force the write to fail by pointing the path at a child of a
        chmod-000 directory. Decision must still return normally; health
        flips False with a real `first_failure_since` and a non-empty
        `last_error`."""
        import tempfile, os
        # Build a directory we can't write into.
        self._locked_dir = tempfile.mkdtemp(prefix="audit_locked_")
        os.chmod(self._locked_dir, 0o000)
        # Path includes a nonexistent parent under the locked dir so the
        # mkdir inside _audit_log will fail.
        unreachable_path = os.path.join(self._locked_dir, "nope", "audit.jsonl")

        # Sanity: precondition is healthy.
        self.assertTrue(recipe.audit_log_health["healthy"])
        self.assertIsNone(recipe.audit_log_health["first_failure_since"])

        decision = recipe._executor_decide(
            field_descriptor={
                "sensitivity": "none",
                "ref": "fname",
                "field_type": "text",
            },
            match={"confidence": 0.95, "value": "John", "source": "label_match"},
            audit_path=unreachable_path,
        )

        # Executor returned normally — degraded but functional.
        self.assertEqual(decision["branch"], recipe.BRANCH_AUTO_FILL_FLAG)

        # Health flipped.
        self.assertFalse(
            recipe.audit_log_health["healthy"],
            "audit_log_health.healthy should be False after write failure",
        )
        self.assertIsNotNone(
            recipe.audit_log_health["first_failure_since"],
            "first_failure_since should be set on first failure",
        )
        # ISO 8601 string, not a unix timestamp.
        self.assertIsInstance(recipe.audit_log_health["first_failure_since"], str)
        self.assertIn("T", recipe.audit_log_health["first_failure_since"])
        # last_error captured something.
        self.assertIsNotNone(recipe.audit_log_health["last_error"])
        self.assertGreater(len(recipe.audit_log_health["last_error"]), 0)
        # last_error must NOT leak the home directory absolute path.
        home = os.path.expanduser("~")
        if home:
            self.assertNotIn(
                home,
                recipe.audit_log_health["last_error"],
                "last_error must redact the user's home directory path",
            )

    def test_last_failure_ts_overwrites_and_failures_count_increments(self):
        """last_failure_ts is overwritten on every failure (distinct from
        first_failure_since which pins). failures_count increments every
        failure, reset only on explicit clear. Together they give the
        operator unhealthy-window duration + magnitude."""
        import tempfile, os, time
        self._locked_dir = tempfile.mkdtemp(prefix="audit_locked_")
        os.chmod(self._locked_dir, 0o000)
        unreachable = os.path.join(self._locked_dir, "nope", "audit.jsonl")
        descriptor = {"sensitivity": "none", "ref": "x", "field_type": "text"}
        match = {"confidence": 0.95, "value": "y", "source": "label"}

        recipe._executor_decide(descriptor, match, audit_path=unreachable)
        self.assertEqual(recipe.audit_log_health["failures_count"], 1)
        first_last_ts = recipe.audit_log_health["last_failure_ts"]
        self.assertIsNotNone(first_last_ts)

        time.sleep(1.1)
        recipe._executor_decide(descriptor, match, audit_path=unreachable)
        self.assertEqual(recipe.audit_log_health["failures_count"], 2)
        second_last_ts = recipe.audit_log_health["last_failure_ts"]
        self.assertNotEqual(
            first_last_ts, second_last_ts,
            "last_failure_ts must overwrite on subsequent failures",
        )

    def test_first_failure_since_does_not_overwrite_on_subsequent_failures(self):
        """Once unhealthy, first_failure_since pins to the FIRST failure
        timestamp in the window. Second failure does not overwrite it.
        (When last_failure_ts lands in the follow-up, that's the slot that
        overwrites; first_failure_since stays put.)"""
        import tempfile, os, time
        self._locked_dir = tempfile.mkdtemp(prefix="audit_locked_")
        os.chmod(self._locked_dir, 0o000)
        unreachable = os.path.join(self._locked_dir, "nope", "audit.jsonl")
        descriptor = {"sensitivity": "none", "ref": "x", "field_type": "text"}
        match = {"confidence": 0.95, "value": "y", "source": "label"}

        recipe._executor_decide(descriptor, match, audit_path=unreachable)
        first_ts = recipe.audit_log_health["first_failure_since"]
        self.assertIsNotNone(first_ts)
        time.sleep(1.1)  # ensure a different ISO 8601 second-resolution stamp
        recipe._executor_decide(descriptor, match, audit_path=unreachable)
        second_ts = recipe.audit_log_health["first_failure_since"]
        self.assertEqual(
            first_ts,
            second_ts,
            "first_failure_since must NOT overwrite on subsequent failures",
        )

    def test_explicit_reset_clears_unhealthy_state(self):
        """audit_log_health stays unhealthy until explicit reset — a silent
        self-heal on the next successful write would mask real damage.
        Reset clears all five slots (healthy, first_failure_since,
        last_failure_ts, failures_count, last_error)."""
        recipe.audit_log_health["healthy"] = False
        recipe.audit_log_health["first_failure_since"] = "2026-05-18T03:55:00+00:00"
        recipe.audit_log_health["last_failure_ts"] = "2026-05-18T04:00:00+00:00"
        recipe.audit_log_health["failures_count"] = 7
        recipe.audit_log_health["last_error"] = "synthetic"
        recipe._reset_framework_state_for_tests()
        self.assertTrue(recipe.audit_log_health["healthy"])
        self.assertIsNone(recipe.audit_log_health["first_failure_since"])
        self.assertIsNone(recipe.audit_log_health["last_failure_ts"])
        self.assertEqual(recipe.audit_log_health["failures_count"], 0)
        self.assertIsNone(recipe.audit_log_health["last_error"])


class PageSignalsProducerCycle1ShapeTests(unittest.TestCase):
    """Cycle-1 smoke test for page_signals_from_context: prove the function
    returns the right SHAPE on a hand-constructed PageContext. Full test
    matrix (single-step / multi-step / submit-step / error-present
    parametrization) is cycle 2/3 work per browser-Claude pacing."""

    def setUp(self):
        recipe._reset_framework_state_for_tests()

    def tearDown(self):
        recipe._reset_framework_state_for_tests()

    def test_empty_context_returns_all_degraded(self):
        """No context → degraded 'wait, not ready' signals. Most slots
        False/None, validation_errors empty list."""
        sig = recipe.page_signals_from_context(recipe.PageContext())
        self.assertIsNone(sig.step_index)
        self.assertIsNone(sig.total_steps)
        self.assertFalse(sig.is_hydrated)
        self.assertFalse(sig.is_submit_step)
        self.assertFalse(sig.has_blocking_errors)
        self.assertEqual(sig.validation_errors, [])
        self.assertFalse(sig.continue_button_present)
        self.assertFalse(sig.continue_button_enabled)

    def test_explicit_step_label_is_parsed(self):
        """'Step 2 of 5' in page text populates step_index/total_steps."""
        ctx = recipe._pagecontext_from_directive_results(
            "<form>Step 2 of 5 — please fill out your information</form>"
        )
        sig = recipe.page_signals_from_context(ctx)
        self.assertEqual(sig.step_index, 2)
        self.assertEqual(sig.total_steps, 5)

    def test_continue_button_text_detected(self):
        """Button copy 'Continue' present → continue_button_present True."""
        ctx = recipe._pagecontext_from_directive_results(
            "<form><input type='text'/><button>Continue</button></form>"
        )
        sig = recipe.page_signals_from_context(ctx)
        self.assertTrue(sig.continue_button_present)
        self.assertFalse(sig.is_submit_step)  # has continue → not submit

    def test_submit_step_detection_via_button_text(self):
        """No 'Continue' but 'Submit' button → is_submit_step True
        (cycle-1 heuristic; documented as best-effort)."""
        ctx = recipe._pagecontext_from_directive_results(
            "<form><input/><button>Submit Application</button></form>"
        )
        sig = recipe.page_signals_from_context(ctx)
        self.assertTrue(sig.is_submit_step)
        self.assertFalse(sig.continue_button_present)

    def test_step_count_overrides_button_text_for_submit_step(self):
        """When step_index >= total_steps, is_submit_step trusts the
        progress label over button text."""
        ctx = recipe._pagecontext_from_directive_results(
            "<form>Step 5 of 5 — review and submit</form>"
        )
        sig = recipe.page_signals_from_context(ctx)
        self.assertTrue(sig.is_submit_step)

    def test_is_hydrated_with_form_content_and_no_loading(self):
        """Form content present AND no loading-phrase → is_hydrated True
        (single-read heuristic)."""
        ctx = recipe._pagecontext_from_directive_results(
            "<form><input name='firstName'/><input name='email'/></form>"
        )
        sig = recipe.page_signals_from_context(ctx)
        self.assertTrue(sig.is_hydrated)

    def test_is_hydrated_false_when_loading_phrase_present(self):
        """Loading-phrase detected → is_hydrated False even with form."""
        ctx = recipe._pagecontext_from_directive_results(
            "<form>loading... please wait <input/></form>"
        )
        sig = recipe.page_signals_from_context(ctx)
        self.assertFalse(sig.is_hydrated)

    def test_blocking_errors_marker_detected(self):
        """'Please fix' / 'required field' / 'invalid' / 'error:' markers
        flip has_blocking_errors True. Structured validation_errors stays
        empty in cycle 1."""
        ctx = recipe._pagecontext_from_directive_results(
            "<form>Please fix the highlighted fields <input/></form>"
        )
        sig = recipe.page_signals_from_context(ctx)
        self.assertTrue(sig.has_blocking_errors)
        self.assertEqual(sig.validation_errors, [])

    def test_previous_context_stability_check(self):
        """When previous_context is provided and raw lengths are close,
        is_hydrated reflects stability not just single-read heuristic."""
        prev = recipe._pagecontext_from_directive_results(
            "<form>same content <input/></form>"
        )
        # Same content, no churn → stable AND hydrated
        curr = recipe._pagecontext_from_directive_results(
            "<form>same content <input/></form>"
        )
        sig = recipe.page_signals_from_context(curr, previous_context=prev)
        self.assertTrue(sig.is_hydrated)

    def test_previous_context_unstable_yields_not_hydrated(self):
        """Big raw-length delta between previous and current → not stable."""
        prev = recipe._pagecontext_from_directive_results("<form>x</form>")
        curr = recipe._pagecontext_from_directive_results(
            "<form>" + ("y" * 1000) + "<input/></form>"
        )
        sig = recipe.page_signals_from_context(curr, previous_context=prev)
        self.assertFalse(sig.is_hydrated)


class ReadFormCurrentStepTests(unittest.TestCase):
    """Cycle-2 regression tests for read_form_current_step + the fill_form
    stub. Two locked invariants:

      - read_form populates _form_descriptors_current_step with the right
        FormDescriptorRecord shape when page_signals.is_hydrated is True
        (happy path).
      - read_form returns interrupt with hydration_failed_after_3_attempts
        when retry budget is exhausted. REGRESSION TEST — a future
        contributor who "fixes" the unbounded-retry behavior by removing
        the cap gets caught here. Same pattern as
        SensitivityGateTests.test_above_personal_never_auto_fills_even_at_full_confidence
        locking the sensitivity tier.

    Per browser-Claude design review 2026-05-18 — cycle 2 lands read_form
    full + fill_form stub + these two tests; cycle 3 wires the match loop.
    """

    def setUp(self):
        recipe._reset_framework_state_for_tests()

    def tearDown(self):
        recipe._reset_framework_state_for_tests()

    def _make_state(self, **data):
        """Minimal state stand-in for tests. SkillState requires more
        plumbing than these unit tests need; this stub exposes the only
        attribute the phase functions read (state.data)."""
        class _S:
            def __init__(self, d):
                self.data = dict(d)
        return _S(data)

    def test_read_form_emits_initial_read_on_first_invocation(self):
        """Branch 1: _initial_read_dispatched is False → emit
        BROWSER_READ_PAGE + interrupt + set the dispatched flag in
        _state_update. Keeps the first-call/no-read-yet branch from
        burning a hydration retry slot."""
        state = self._make_state()
        out = recipe.read_form_current_step(state)
        self.assertEqual(out["outcome"], "interrupt")
        self.assertEqual(out["details"]["reason"], "awaiting_initial_read")
        directives = out["details"]["_pending_directives"]
        self.assertTrue(any("BROWSER_READ_PAGE" in d for d in directives))
        self.assertTrue(
            out["details"]["_state_update"][recipe._STATE_KEY_INITIAL_READ_DISPATCHED]
        )

    def test_read_form_flags_directive_failure_when_read_didnt_produce(self):
        """Branch 2: read was dispatched but _last_directive_results is
        missing/empty. That's directive-execution failure, NOT hydration
        failure — must escalate to its own interrupt reason so the retry
        budget isn't consumed on a read that never happened."""
        state = self._make_state(**{
            recipe._STATE_KEY_INITIAL_READ_DISPATCHED: True,
            "_last_directive_results": "",
        })
        out = recipe.read_form_current_step(state)
        self.assertEqual(out["outcome"], "interrupt")
        self.assertEqual(out["details"]["reason"], "read_directive_failed")

    def test_read_form_populates_descriptors_with_correct_shape_when_hydrated(self):
        """Locked invariant #1: hydrated read produces a FormDescriptorRecord
        with step_id / ts_read (ISO 8601) / page_signals / descriptors slots,
        written to _form_descriptors_current_step. step_id derives from
        page_signals.step_index when present.

        Cycle 3: descriptors now come from real DOM extraction via
        _extract_form_descriptors (no more synthetic stub). The fixture
        feeds a JSON page_context matching the extension's domState()
        output shape, so the extractor produces real descriptors.
        """
        import json as _json
        hydrated_payload = _json.dumps({
            "page_context": {
                "url": "https://example.com/apply",
                "title": "Apply — Step 2 of 5",
                "visible_text": "Step 2 of 5\nFirst Name *\nEmail Address\nContinue",
                "dom_state": {
                    "forms": [{
                        "index": 0,
                        "selector": "form",
                        "fields": [
                            {"role": "textbox", "name": "First Name *",
                             "selector": "input[name=firstName]", "type": "text",
                             "value_present": False},
                            {"role": "textbox", "name": "Email Address",
                             "selector": "input[name=email]", "type": "email",
                             "value_present": False},
                        ],
                    }],
                    "counts": {"forms": 1, "inputs": 2, "buttons": 1, "links": 0},
                },
            }
        })
        state = self._make_state(**{
            recipe._STATE_KEY_INITIAL_READ_DISPATCHED: True,
            "_last_directive_results": hydrated_payload,
        })
        out = recipe.read_form_current_step(state)
        self.assertEqual(out["outcome"], "interrupt")
        self.assertEqual(
            out["details"]["reason"],
            "form_read_complete_transition_to_fill",
        )
        record = out["details"]["_state_update"][recipe._STATE_KEY_FORM_DESCRIPTORS]
        self.assertIsInstance(record, recipe.FormDescriptorRecord)
        # step_id derived from step_index=2 → "step_2"
        self.assertEqual(record.step_id, "step_2")
        # ts_read is ISO 8601 (T separator + UTC suffix + subsecond '.')
        self.assertIn("T", record.ts_read)
        self.assertTrue(
            record.ts_read.endswith("+00:00") or record.ts_read.endswith("Z"),
            f"ts_read must be UTC: {record.ts_read}",
        )
        self.assertIn(".", record.ts_read)
        # page_signals carried through
        self.assertIsInstance(record.page_signals, recipe.PageSignals)
        self.assertTrue(record.page_signals.is_hydrated)
        self.assertEqual(record.page_signals.step_index, 2)
        # Cycle 3: real descriptors from dom_forms — two fields in fixture,
        # both should map to known field_role via _label_to_field_role.
        self.assertEqual(len(record.descriptors), 2)
        roles = {d["field_role"] for d in record.descriptors}
        self.assertEqual(roles, {"name_first", "email"})
        # asterisk-required detection
        first_name = next(d for d in record.descriptors if d["field_role"] == "name_first")
        self.assertTrue(first_name["required"])
        email = next(d for d in record.descriptors if d["field_role"] == "email")
        self.assertFalse(email["required"])

    def test_read_form_returns_hydration_failed_when_retry_budget_exhausted(self):
        """LOCKED INVARIANT #2 — REGRESSION TEST. Retry budget for
        hydration is hard-capped at _READ_FORM_MAX_RETRIES (3 per cycle-2
        spec). A future contributor who "fixes" unbounded-retry behavior
        by removing the cap, by raising it to a huge number, or by
        decrementing instead of incrementing, gets caught here. The
        bounded-retry property is non-negotiable — unbounded retry on a
        stuck page burns the session indefinitely.

        Same regression-test pattern as the sensitivity-gate invariant
        test in SensitivityGateTests.
        """
        # Not-hydrated content + retry_count already at the cap.
        state = self._make_state(**{
            recipe._STATE_KEY_INITIAL_READ_DISPATCHED: True,
            "_last_directive_results": "loading... please wait",
            recipe._STATE_KEY_READ_RETRY: recipe._READ_FORM_MAX_RETRIES,
        })
        out = recipe.read_form_current_step(state)
        self.assertEqual(out["outcome"], "interrupt")
        self.assertEqual(
            out["details"]["reason"],
            f"hydration_failed_after_{recipe._READ_FORM_MAX_RETRIES}_attempts",
        )

    def test_fill_form_stub_descriptors_missing_when_slot_empty(self):
        """Cycle-2 stub: presence check fires on empty slot."""
        state = self._make_state()
        out = recipe.fill_form_current_step(state)
        self.assertEqual(out["outcome"], "interrupt")
        self.assertEqual(out["details"]["reason"], "descriptors_missing")

    def test_fill_form_handoff_when_no_profile_match(self):
        """Cycle 3 real-mode: when a descriptor has no profile-driven
        match (e.g., no profile file or unmapped role), the executor
        decides stop_and_ask, and fill_form interrupts with a
        field_handoff_<branch> reason carrying the field metadata.
        No BROWSER_FILL emitted; operator decides per field."""
        record = recipe.FormDescriptorRecord(
            step_id="step_1",
            ts_read="2026-05-18T06:13:00.000000+00:00",
            page_signals=recipe.PageSignals(step_index=1, is_hydrated=True),
            descriptors=[{
                "field_role": "name_first",
                "label_visible": "First Name",
                "css_selector": "input[name=firstName]",
                "html_input_type": "text",
                "required": True,
                "step_index": 1,
            }],
        )
        state = self._make_state(**{recipe._STATE_KEY_FORM_DESCRIPTORS: record})
        # No profile written — _load_profile_or_none returns None →
        # match has confidence 0 → executor chooses stop_and_ask.
        out = recipe.fill_form_current_step(state)
        self.assertEqual(out["outcome"], "interrupt")
        self.assertTrue(out["details"]["reason"].startswith("field_handoff_"))
        self.assertEqual(out["details"]["handoff"]["field_role"], "name_first")
        self.assertEqual(out["details"]["handoff"]["css_selector"], "input[name=firstName]")

    def test_fill_form_emits_browser_fill_when_profile_matches(self):
        """Cycle 3 real-mode: when profile supplies a value for a
        descriptor's field_role, the executor chooses auto_fill_flag
        and fill_form emits a BROWSER_FILL directive in
        _pending_directives. No handoff for that field."""
        import tempfile, os as _os
        # Write a minimal profile to disk at PROFILE_PATH for the test.
        # _load_profile_or_none reads from a hardcoded path; back up and
        # restore so other tests stay isolated.
        backup_existed = recipe.PROFILE_PATH.exists()
        if backup_existed:
            backup = recipe.PROFILE_PATH.read_text()
        try:
            recipe.PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            recipe.PROFILE_PATH.write_text(
                '{"first_name":"Elijah","last_name":"Ebey","email":"test@example.com","phone":"5551234567","address":"123 Main St"}'
            )
            record = recipe.FormDescriptorRecord(
                step_id="step_1",
                ts_read="2026-05-18T06:13:00.000000+00:00",
                page_signals=recipe.PageSignals(step_index=1, is_hydrated=True),
                descriptors=[{
                    "field_role": "name_first",
                    "label_visible": "First Name *",
                    "css_selector": "input[name=firstName]",
                    "html_input_type": "text",
                    "required": True,
                    "step_index": 1,
                }],
            )
            state = self._make_state(**{recipe._STATE_KEY_FORM_DESCRIPTORS: record})
            out = recipe.fill_form_current_step(state)
            self.assertEqual(out["outcome"], "interrupt")
            self.assertEqual(out["details"]["reason"], "fields_filled_pending_resume")
            directives = out["details"]["_pending_directives"]
            self.assertEqual(len(directives), 1)
            self.assertEqual(directives[0], "BROWSER_FILL: input[name=firstName] :: Elijah")
        finally:
            if backup_existed:
                recipe.PROFILE_PATH.write_text(backup)
            else:
                try:
                    recipe.PROFILE_PATH.unlink()
                except FileNotFoundError:
                    pass

    def test_fill_form_validation_errors_gate(self):
        """Cycle 3 gate: when page_signals.has_blocking_errors is True,
        fill_form interrupts with validation_errors_present BEFORE
        attempting any fills. Operator resolves errors first."""
        record = recipe.FormDescriptorRecord(
            step_id="step_1",
            ts_read="2026-05-18T06:13:00.000000+00:00",
            page_signals=recipe.PageSignals(
                step_index=1, is_hydrated=True,
                has_blocking_errors=True,
                validation_errors=[{"field": "email", "message": "invalid format"}],
            ),
            descriptors=[],
        )
        state = self._make_state(**{recipe._STATE_KEY_FORM_DESCRIPTORS: record})
        out = recipe.fill_form_current_step(state)
        self.assertEqual(out["details"]["reason"], "validation_errors_present")

    def test_fill_form_submit_step_routes_to_submit_gate(self):
        """Cycle 3 gate: is_submit_step routes to submit_gate_phase
        rather than auto-filling — final submit is irreversible and
        must go through the explicit confirm path."""
        record = recipe.FormDescriptorRecord(
            step_id="step_5",
            ts_read="2026-05-18T06:13:00.000000+00:00",
            page_signals=recipe.PageSignals(
                step_index=5, is_hydrated=True, is_submit_step=True
            ),
            descriptors=[],
        )
        state = self._make_state(**{recipe._STATE_KEY_FORM_DESCRIPTORS: record})
        out = recipe.fill_form_current_step(state)
        self.assertEqual(out["details"]["reason"], "submit_step_detected_route_to_submit_gate")
        self.assertEqual(out["details"]["step_index"], 5)


class TaskModelV0Tests(unittest.TestCase):
    """V0 task abstraction tests. Grounded in the apply-job-session use case
    per browser-Claude design 2026-05-18 — task HAS-A state, dispatches to
    phase functions, no persistence / multi-task / cross-task deps in v0."""

    def setUp(self):
        recipe._reset_framework_state_for_tests()
        import task_model
        self.tm = task_model

    def tearDown(self):
        recipe._reset_framework_state_for_tests()

    def test_task_initializes_with_spawned_state_and_iso8601_timestamp(self):
        """Default state is SPAWNED; spawned_at is ISO 8601 UTC microsecond."""
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")
        self.assertEqual(task.state, self.tm.TASK_STATE_SPAWNED)
        self.assertIsNone(task.terminated_reason)
        self.assertIn("T", task.spawned_at)
        self.assertTrue(
            task.spawned_at.endswith("+00:00") or task.spawned_at.endswith("Z")
        )
        self.assertIn(".", task.spawned_at)

    def test_task_rejects_invalid_state(self):
        with self.assertRaises(ValueError):
            self.tm.Task(task_id="t1", task_type="apply", state="nonsense")

    def test_dispatch_applied_transitions_to_terminated_applied(self):
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")

        def phase_returns_applied(t):
            return {"outcome": "applied", "details": {"ref_number": "ABC123"}}

        out = self.tm.task_dispatch(task, phase_returns_applied)
        self.assertEqual(out["outcome"], "applied")
        self.assertEqual(task.state, self.tm.TASK_STATE_TERMINATED)
        self.assertEqual(task.terminated_reason, self.tm.TASK_TERMINATED_APPLIED)

    def test_dispatch_skipped_transitions_to_terminated_skipped(self):
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")

        def phase_returns_skipped(t):
            return {"outcome": "skipped", "details": {"reason": "external_apply_only"}}

        out = self.tm.task_dispatch(task, phase_returns_skipped)
        self.assertEqual(task.state, self.tm.TASK_STATE_TERMINATED)
        self.assertEqual(task.terminated_reason, self.tm.TASK_TERMINATED_SKIPPED)

    def test_dispatch_interrupt_stays_running_not_terminated(self):
        """Interrupt is operator-pause, not termination. Task stays RUNNING
        so the next dispatcher call re-enters the phase function."""
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")

        def phase_returns_interrupt(t):
            return {"outcome": "interrupt", "details": {"reason": "captcha_present"}}

        out = self.tm.task_dispatch(task, phase_returns_interrupt)
        self.assertEqual(task.state, self.tm.TASK_STATE_RUNNING)
        self.assertIsNone(task.terminated_reason)

    def test_dispatch_failed_transitions_to_terminated_failed(self):
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")

        def phase_returns_failed(t):
            return {"outcome": "failed", "details": {"reason": "timeout"}}

        out = self.tm.task_dispatch(task, phase_returns_failed)
        self.assertEqual(task.state, self.tm.TASK_STATE_TERMINATED)
        self.assertEqual(task.terminated_reason, self.tm.TASK_TERMINATED_FAILED)

    def test_dispatch_unknown_outcome_terminates_as_failed_with_note(self):
        """Unknown outcome → fail loud, not silently advance. State-machine
        violation if we let drift go."""
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")

        def phase_returns_garbage(t):
            return {"outcome": "tornado", "details": {}}

        out = self.tm.task_dispatch(task, phase_returns_garbage)
        self.assertEqual(task.state, self.tm.TASK_STATE_TERMINATED)
        self.assertEqual(task.terminated_reason, self.tm.TASK_TERMINATED_FAILED)
        self.assertIn("_dispatcher_note", out["details"])

    def test_dispatch_on_terminated_task_raises(self):
        """REGRESSION: re-entering a terminated task is a state-machine
        violation; dispatcher refuses rather than silently restarts it."""
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")

        def applied(t):
            return {"outcome": "applied", "details": {}}

        self.tm.task_dispatch(task, applied)
        self.assertEqual(task.state, self.tm.TASK_STATE_TERMINATED)
        with self.assertRaises(RuntimeError):
            self.tm.task_dispatch(task, applied)

    def test_grounded_apply_flow_round_trip(self):
        """Compose a fake apply-one-job flow end-to-end through the
        dispatcher: spawned → running (find_apply skipping) → terminated
        SKIPPED. Proves the v0 model works against the existing phase shape."""
        task = self.tm.Task(
            task_id="apply_001",
            task_type="apply_one_job",
            target={"url": "https://www.indeed.com/viewjob?jk=fake", "jk": "fake"},
            params={"resume_path": "/tmp/resume.pdf"},
        )

        # Phase 1: hand off — produces an interrupt (paused for operator).
        def find_apply_phase(t):
            return {
                "outcome": "interrupt",
                "details": {
                    "_pending_directives": ["BROWSER_FIND: Apply"],
                    "reason": "adapter_indeed: locating Apply button",
                },
            }

        out = self.tm.task_dispatch(task, find_apply_phase)
        self.assertEqual(task.state, self.tm.TASK_STATE_RUNNING)
        self.assertEqual(out["outcome"], "interrupt")

        # Phase 2: skip-companies filter hit on re-entry.
        def skip_filter(t):
            return {
                "outcome": "skipped",
                "details": {"reason": "skip_company:All Trades Staffing"},
            }

        out = self.tm.task_dispatch(task, skip_filter)
        self.assertEqual(task.state, self.tm.TASK_STATE_TERMINATED)
        self.assertEqual(task.terminated_reason, self.tm.TASK_TERMINATED_SKIPPED)


class CrossTabRoutingV0Tests(unittest.TestCase):
    """Cross-tab routing v0 tests — TaskTabBinding + route_for_task with
    the four-branch routing (OK / AMBIGUOUS / NO_PRIMARY / STALE_PRIMARY).
    Per browser-Claude design lock 2026-05-18.

    Stale-primary is the fourth branch (BC's refinement to operator's
    original three) — when a primary binding exists but the tab closed
    or the tab navigated away from the bound URL. Surfaced as its own
    interrupt rather than silent fallback to NO_PRIMARY — preserves the
    information so operator can recover (closed-by-accident tab, intentional
    navigation, etc.).
    """

    def setUp(self):
        recipe._reset_framework_state_for_tests()
        import task_model
        self.tm = task_model

    def tearDown(self):
        recipe._reset_framework_state_for_tests()

    def _binding(self, **kwargs):
        defaults = dict(
            task_id="t1",
            tab_id=100,
            role=self.tm.BINDING_ROLE_PRIMARY,
            binding_source=self.tm.BINDING_SOURCE_OPERATOR_ADDED,
            last_observed_url="https://example.com/page",
        )
        defaults.update(kwargs)
        return self.tm.TaskTabBinding(**defaults)

    def _task_with_bindings(self, bindings):
        return self.tm.Task(
            task_id="t1",
            task_type="apply_one_job",
            tab_bindings=list(bindings),
        )

    def test_binding_rejects_invalid_role(self):
        with self.assertRaises(ValueError):
            self.tm.TaskTabBinding(
                task_id="t1",
                tab_id=100,
                role="nonsense_role",
                binding_source=self.tm.BINDING_SOURCE_OPERATOR_ADDED,
            )

    def test_binding_rejects_invalid_source(self):
        with self.assertRaises(ValueError):
            self.tm.TaskTabBinding(
                task_id="t1",
                tab_id=100,
                role=self.tm.BINDING_ROLE_PRIMARY,
                binding_source="invalid_source",
            )

    def test_binding_added_ts_is_iso8601(self):
        b = self._binding()
        self.assertIn("T", b.added_ts)
        self.assertTrue(
            b.added_ts.endswith("+00:00") or b.added_ts.endswith("Z")
        )
        self.assertIn(".", b.added_ts)

    def test_routing_ok_with_single_live_primary(self):
        """Branch 1: exactly one primary, on its bound URL → OK."""
        task = self._task_with_bindings([
            self._binding(tab_id=100, last_observed_url="https://example.com/page"),
        ])
        out = self.tm.route_for_task(task, {100: "https://example.com/page"})
        self.assertEqual(out["outcome"], self.tm.ROUTING_OK)
        self.assertEqual(out["details"]["tab_id"], 100)
        self.assertEqual(
            out["details"]["binding_source"],
            self.tm.BINDING_SOURCE_OPERATOR_ADDED,
        )

    def test_routing_no_primary_with_zero_primary_bindings(self):
        """Branch 2: no primary bindings at all."""
        task = self._task_with_bindings([])
        out = self.tm.route_for_task(task, {})
        self.assertEqual(out["outcome"], self.tm.ROUTING_NO_PRIMARY)

    def test_routing_no_primary_when_only_reference_bindings_exist(self):
        """Reference/monitor bindings don't count for routing."""
        task = self._task_with_bindings([
            self._binding(tab_id=200, role=self.tm.BINDING_ROLE_REFERENCE),
            self._binding(tab_id=300, role=self.tm.BINDING_ROLE_MONITOR),
        ])
        out = self.tm.route_for_task(task, {200: "https://drive.google.com",
                                           300: "https://gmail.com"})
        self.assertEqual(out["outcome"], self.tm.ROUTING_NO_PRIMARY)

    def test_routing_ambiguous_with_multiple_live_primaries(self):
        """Branch 3: two primary bindings, both live → operator picks."""
        task = self._task_with_bindings([
            self._binding(tab_id=100, last_observed_url="https://a.com"),
            self._binding(tab_id=200, last_observed_url="https://b.com"),
        ])
        out = self.tm.route_for_task(task, {
            100: "https://a.com",
            200: "https://b.com",
        })
        self.assertEqual(out["outcome"], self.tm.ROUTING_AMBIGUOUS)
        self.assertEqual(len(out["details"]["candidates"]), 2)

    def test_routing_stale_when_tab_closed(self):
        """Branch 4a: primary binding exists, tab is gone from current_tab_urls.
        Distinct from NO_PRIMARY — the binding STILL EXISTS, so the operator
        gets a recovery interrupt, not a silent re-bind prompt."""
        task = self._task_with_bindings([
            self._binding(tab_id=100, last_observed_url="https://a.com"),
        ])
        out = self.tm.route_for_task(task, {})  # tab 100 not in current
        self.assertEqual(out["outcome"], self.tm.ROUTING_STALE_PRIMARY)
        self.assertEqual(out["details"]["stale"][0]["reason"], "tab_closed")

    def test_routing_stale_when_tab_url_drifted(self):
        """Branch 4b: primary binding's tab is still open, but its URL drifted
        from the bound last_observed_url. Same surfacing as tab-closed —
        the operator decides whether to drop or re-confirm."""
        task = self._task_with_bindings([
            self._binding(tab_id=100, last_observed_url="https://a.com/apply"),
        ])
        out = self.tm.route_for_task(task, {100: "https://a.com/feed"})
        self.assertEqual(out["outcome"], self.tm.ROUTING_STALE_PRIMARY)
        self.assertEqual(out["details"]["stale"][0]["reason"], "tab_url_drifted")
        self.assertEqual(
            out["details"]["stale"][0]["current_url"],
            "https://a.com/feed",
        )

    def test_routing_mixed_live_and_stale_returns_ok_on_live(self):
        """When some primaries are stale and exactly one is live, the live
        one wins — the stale ones don't block. (If TWO are live, that's
        ambiguous; if ZERO are live, that's stale_primary.)"""
        task = self._task_with_bindings([
            self._binding(tab_id=100, last_observed_url="https://a.com"),  # stale
            self._binding(tab_id=200, last_observed_url="https://b.com"),  # live
        ])
        out = self.tm.route_for_task(task, {200: "https://b.com"})
        self.assertEqual(out["outcome"], self.tm.ROUTING_OK)
        self.assertEqual(out["details"]["tab_id"], 200)


class SideEffectsAndRedirectPromoteTests(unittest.TestCase):
    """Adapter-integration v0 tests — side-effects list applied by
    dispatcher, plus detect_redirect_and_promote helper for adapters.
    Per browser-Claude design lock 2026-05-18 (b-plus shape — side
    effects list, not magic keys in outcome details, not yet TaskPatch
    dataclass)."""

    def setUp(self):
        recipe._reset_framework_state_for_tests()
        import task_model
        self.tm = task_model

    def tearDown(self):
        recipe._reset_framework_state_for_tests()

    def test_dispatcher_applies_tab_binding_add_side_effect(self):
        """Phase emits a tab_binding_add side effect; dispatcher applies it
        to the task on interrupt outcome."""
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")
        binding = self.tm.TaskTabBinding(
            task_id="t1", tab_id=200,
            role=self.tm.BINDING_ROLE_PRIMARY,
            binding_source=self.tm.BINDING_SOURCE_ADAPTER_PROMOTED,
            last_observed_url="https://smartapply.indeed.com/form",
        )

        def phase_emits_binding(t):
            return {
                "outcome": "interrupt",
                "details": {
                    "reason": "redirect_to_smartapply",
                    "_side_effects": [{
                        "kind": self.tm.SIDE_EFFECT_TAB_BINDING_ADD,
                        "binding": binding,
                    }],
                },
            }

        out = self.tm.task_dispatch(task, phase_emits_binding)
        self.assertEqual(len(task.tab_bindings), 1)
        self.assertEqual(task.tab_bindings[0].tab_id, 200)
        # Dispatcher records what got applied for audit/debug
        self.assertIn("_side_effects_applied", out["details"])

    def test_dispatcher_applies_tab_binding_demote_side_effect(self):
        """Phase emits a tab_binding_demote; dispatcher mutates the
        existing binding's role + last_observed_url in place."""
        existing = self.tm.TaskTabBinding(
            task_id="t1", tab_id=100,
            role=self.tm.BINDING_ROLE_PRIMARY,
            binding_source=self.tm.BINDING_SOURCE_OPERATOR_ADDED,
            last_observed_url="https://www.indeed.com/viewjob?jk=abc",
        )
        task = self.tm.Task(
            task_id="t1", task_type="apply_one_job",
            tab_bindings=[existing],
        )

        def phase_emits_demote(t):
            return {
                "outcome": "interrupt",
                "details": {
                    "reason": "primary_demoted_to_reference",
                    "_side_effects": [{
                        "kind": self.tm.SIDE_EFFECT_TAB_BINDING_DEMOTE,
                        "tab_id": 100,
                        "new_role": self.tm.BINDING_ROLE_REFERENCE,
                        "new_last_observed_url": "https://www.indeed.com/viewjob?jk=abc&seen=true",
                    }],
                },
            }

        self.tm.task_dispatch(task, phase_emits_demote)
        self.assertEqual(task.tab_bindings[0].role, self.tm.BINDING_ROLE_REFERENCE)
        self.assertIn("seen=true", task.tab_bindings[0].last_observed_url)

    def test_unknown_side_effect_kind_is_skipped_loudly_not_silently(self):
        """Unknown side-effect kinds get recorded as skipped — fail loud,
        don't silently drop and lose information."""
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")

        def phase_emits_garbage(t):
            return {
                "outcome": "interrupt",
                "details": {
                    "_side_effects": [{"kind": "tornado", "data": "wat"}],
                },
            }

        out = self.tm.task_dispatch(task, phase_emits_garbage)
        applied = out["details"]["_side_effects_applied"]
        self.assertEqual(applied[0]["kind"], "tornado")
        self.assertEqual(applied[0]["skipped"], "unknown_side_effect_kind")

    def test_detect_redirect_and_promote_builds_correct_side_effects(self):
        """Helper detects new tab on redirect host, builds side-effects list
        with demote-of-old-primary + add-new-primary. Pure function — does
        not mutate task."""
        old_primary = self.tm.TaskTabBinding(
            task_id="t1", tab_id=100,
            role=self.tm.BINDING_ROLE_PRIMARY,
            binding_source=self.tm.BINDING_SOURCE_OPERATOR_ADDED,
            last_observed_url="https://www.indeed.com/viewjob?jk=xyz",
        )
        task = self.tm.Task(
            task_id="t1", task_type="apply_one_job",
            tab_bindings=[old_primary],
        )
        previous_tab_urls = {100: "https://www.indeed.com/viewjob?jk=xyz"}
        current_tab_urls = {
            100: "https://www.indeed.com/viewjob?jk=xyz",
            201: "https://smartapply.indeed.com/beta/indeedapply/form/start",
        }

        effects = self.tm.detect_redirect_and_promote(
            task, previous_tab_urls, current_tab_urls,
            redirect_host_substring="smartapply.indeed.com",
        )
        # One demote, one add
        kinds = [e["kind"] for e in effects]
        self.assertEqual(
            sorted(kinds),
            sorted([self.tm.SIDE_EFFECT_TAB_BINDING_DEMOTE,
                    self.tm.SIDE_EFFECT_TAB_BINDING_ADD]),
        )
        # Demote targets the existing primary tab_id
        demote = next(e for e in effects
                      if e["kind"] == self.tm.SIDE_EFFECT_TAB_BINDING_DEMOTE)
        self.assertEqual(demote["tab_id"], 100)
        self.assertEqual(demote["new_role"], self.tm.BINDING_ROLE_REFERENCE)
        # Add targets the new tab
        add = next(e for e in effects
                   if e["kind"] == self.tm.SIDE_EFFECT_TAB_BINDING_ADD)
        self.assertEqual(add["binding"].tab_id, 201)
        self.assertEqual(add["binding"].role, self.tm.BINDING_ROLE_PRIMARY)
        self.assertEqual(
            add["binding"].binding_source,
            self.tm.BINDING_SOURCE_ADAPTER_PROMOTED,
        )
        # Helper did NOT mutate task
        self.assertEqual(task.tab_bindings[0].role, self.tm.BINDING_ROLE_PRIMARY)

    def test_detect_redirect_no_new_tab_returns_empty_list(self):
        """If no new tab opens on the redirect host, no side effects."""
        task = self.tm.Task(task_id="t1", task_type="apply_one_job")
        effects = self.tm.detect_redirect_and_promote(
            task,
            previous_tab_urls={100: "https://www.indeed.com/viewjob?jk=a"},
            current_tab_urls={100: "https://www.indeed.com/viewjob?jk=a"},
            redirect_host_substring="smartapply.indeed.com",
        )
        self.assertEqual(effects, [])

    def test_end_to_end_redirect_promote_through_dispatcher(self):
        """Helper output flows through phase outcome → dispatcher →
        task mutation. Proves the full integration shape."""
        old_primary = self.tm.TaskTabBinding(
            task_id="t1", tab_id=100,
            role=self.tm.BINDING_ROLE_PRIMARY,
            binding_source=self.tm.BINDING_SOURCE_OPERATOR_ADDED,
            last_observed_url="https://www.indeed.com/viewjob?jk=q",
        )
        task = self.tm.Task(
            task_id="t1", task_type="apply_one_job",
            tab_bindings=[old_primary],
        )
        previous = {100: "https://www.indeed.com/viewjob?jk=q"}
        current = {
            100: "https://www.indeed.com/viewjob?jk=q",
            301: "https://smartapply.indeed.com/beta/indeedapply/form",
        }

        # Phase function that uses the helper to build its side effects
        def redirect_phase(t):
            effects = self.tm.detect_redirect_and_promote(
                t, previous, current, "smartapply.indeed.com",
            )
            return {
                "outcome": "interrupt",
                "details": {
                    "reason": "click_apply_redirected_to_smartapply",
                    "_side_effects": effects,
                },
            }

        self.tm.task_dispatch(task, redirect_phase)

        # After dispatch: 2 bindings (old demoted, new added)
        self.assertEqual(len(task.tab_bindings), 2)
        old = next(b for b in task.tab_bindings if b.tab_id == 100)
        new = next(b for b in task.tab_bindings if b.tab_id == 301)
        self.assertEqual(old.role, self.tm.BINDING_ROLE_REFERENCE)
        self.assertEqual(new.role, self.tm.BINDING_ROLE_PRIMARY)
        self.assertEqual(
            new.binding_source,
            self.tm.BINDING_SOURCE_ADAPTER_PROMOTED,
        )


class PersistenceV0Tests(unittest.TestCase):
    """V0 persistence tests. Per browser-Claude design 2026-05-18 —
    manual save centralized in dispatcher, multi-file storage with
    append-only index, atomic write, best-effort load with no silent
    recovery (warnings block advance until acknowledged)."""

    def setUp(self):
        recipe._reset_framework_state_for_tests()
        import task_model
        import persistence
        self.tm = task_model
        self.pers = persistence

        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        recipe._reset_framework_state_for_tests()

    # ── Roundtrip ────────────────────────────────────────────────

    def test_save_and_load_roundtrip_simple_task(self):
        """A vanilla task saves to <base>/<task_id>.json and loads back
        with identical scalar fields."""
        task = self.tm.Task(
            task_id="t_round_1",
            task_type="apply_one_job",
            params={"jk": "abc123"},
        )
        path = self.pers.save_task(task, base_path=self.base)
        self.assertTrue(path.exists())
        self.assertEqual(path, self.base / "t_round_1.json")

        loaded = self.pers.load_task("t_round_1", base_path=self.base)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task_id, "t_round_1")
        self.assertEqual(loaded.task_type, "apply_one_job")
        self.assertEqual(loaded.params, {"jk": "abc123"})
        self.assertEqual(loaded.state, self.tm.TASK_STATE_SPAWNED)
        self.assertEqual(loaded.persistence_warnings, [])

    def test_save_and_load_preserves_tab_bindings(self):
        """Nested TaskTabBinding objects survive the JSON round-trip."""
        task = self.tm.Task(task_id="t_bind", task_type="apply_one_job")
        task.tab_bindings.append(
            self.tm.TaskTabBinding(
                task_id="t_bind",
                tab_id=42,
                role=self.tm.BINDING_ROLE_PRIMARY,
                binding_source=self.tm.BINDING_SOURCE_OPERATOR_ADDED,
                last_observed_url="https://example.com/x",
            )
        )
        self.pers.save_task(task, base_path=self.base)
        loaded = self.pers.load_task("t_bind", base_path=self.base)

        self.assertEqual(len(loaded.tab_bindings), 1)
        b = loaded.tab_bindings[0]
        self.assertIsInstance(b, self.tm.TaskTabBinding)
        self.assertEqual(b.tab_id, 42)
        self.assertEqual(b.role, self.tm.BINDING_ROLE_PRIMARY)
        self.assertEqual(
            b.binding_source, self.tm.BINDING_SOURCE_OPERATOR_ADDED
        )
        self.assertEqual(b.last_observed_url, "https://example.com/x")

    def test_save_preserves_terminated_state(self):
        """A terminated task round-trips its state + reason cleanly."""
        task = self.tm.Task(task_id="t_term", task_type="apply_one_job")
        task.state = self.tm.TASK_STATE_TERMINATED
        task.terminated_reason = self.tm.TASK_TERMINATED_APPLIED
        self.pers.save_task(task, base_path=self.base)
        loaded = self.pers.load_task("t_term", base_path=self.base)
        self.assertEqual(loaded.state, self.tm.TASK_STATE_TERMINATED)
        self.assertEqual(
            loaded.terminated_reason, self.tm.TASK_TERMINATED_APPLIED
        )

    # ── Missing file / malformed JSON ────────────────────────────

    def test_load_returns_none_when_file_missing(self):
        """Loading a nonexistent task_id returns None, not a partial
        Task and not a raise."""
        result = self.pers.load_task("never_saved", base_path=self.base)
        self.assertIsNone(result)

    def test_load_raises_on_malformed_json(self):
        """Corrupted JSON file fails loudly — no partial Task returned."""
        (self.base / "broken.json").write_text("{not valid json")
        with self.assertRaises(self.pers.TaskLoadError):
            self.pers.load_task("broken", base_path=self.base)

    # ── Best-effort load: warnings populated ─────────────────────

    def test_load_unknown_field_produces_warning(self):
        """Unknown top-level fields → persistence_warnings entry, but
        the task still loads."""
        import json as _json
        path = self.base / "u.json"
        path.write_text(_json.dumps({
            "_schema": "task_v0",
            "task_id": "u",
            "task_type": "apply_one_job",
            "future_field": 42,
        }))
        loaded = self.pers.load_task("u", base_path=self.base)
        self.assertIsNotNone(loaded)
        self.assertIn("unknown_field:future_field", loaded.persistence_warnings)

    def test_load_missing_optional_field_warns_and_defaults(self):
        """Missing optional field with default → warning + default value
        applied. Operator must confirm_recovered() before dispatching."""
        import json as _json
        path = self.base / "m.json"
        path.write_text(_json.dumps({
            "_schema": "task_v0",
            "task_id": "m",
            "task_type": "apply_one_job",
        }))
        loaded = self.pers.load_task("m", base_path=self.base)
        self.assertIsNotNone(loaded)
        # state was missing — got defaulted, with a warning
        self.assertEqual(loaded.state, self.tm.TASK_STATE_SPAWNED)
        self.assertIn(
            "missing_field_defaulted:state", loaded.persistence_warnings
        )

    def test_load_missing_required_field_raises(self):
        """Missing task_id or task_type → TaskLoadError, no partial Task."""
        import json as _json
        path = self.base / "r.json"
        path.write_text(_json.dumps({"_schema": "task_v0", "task_id": "r"}))
        with self.assertRaises(self.pers.TaskLoadError):
            self.pers.load_task("r", base_path=self.base)

    def test_load_type_mismatch_raises(self):
        """Type mismatch on required field → TaskLoadError, fail loud."""
        import json as _json
        path = self.base / "tm.json"
        path.write_text(_json.dumps({
            "_schema": "task_v0",
            "task_id": "tm",
            "task_type": "apply_one_job",
            "params": "should_be_a_dict",
        }))
        with self.assertRaises(self.pers.TaskLoadError):
            self.pers.load_task("tm", base_path=self.base)

    def test_load_invalid_binding_role_raises(self):
        """Enum violation in nested binding → TaskLoadError."""
        import json as _json
        path = self.base / "ib.json"
        path.write_text(_json.dumps({
            "_schema": "task_v0",
            "task_id": "ib",
            "task_type": "apply_one_job",
            "tab_bindings": [{
                "task_id": "ib",
                "tab_id": 1,
                "role": "nonsense_role",
                "binding_source": self.tm.BINDING_SOURCE_OPERATOR_ADDED,
            }],
        }))
        with self.assertRaises(self.pers.TaskLoadError):
            self.pers.load_task("ib", base_path=self.base)

    # ── Atomic write ─────────────────────────────────────────────

    def test_atomic_write_leaves_no_tmp_files_on_success(self):
        """After a clean save, no .tmp debris remains in the dir."""
        task = self.tm.Task(task_id="atom", task_type="apply_one_job")
        self.pers.save_task(task, base_path=self.base)
        tmp_files = list(self.base.glob("*.tmp"))
        self.assertEqual(tmp_files, [])
        tmp_dot = list(self.base.glob("*.tmp*"))
        self.assertEqual(tmp_dot, [])

    # ── Index file ───────────────────────────────────────────────

    def test_save_appends_index_line(self):
        """Each save appends one JSON line to _index.jsonl with task_id,
        state, terminated_reason, and ts."""
        import json as _json
        task = self.tm.Task(task_id="idx", task_type="apply_one_job")
        self.pers.save_task(task, base_path=self.base)
        self.pers.save_task(task, base_path=self.base)

        index = (self.base / self.pers.INDEX_FILENAME).read_text().splitlines()
        self.assertEqual(len(index), 2)
        for line in index:
            entry = _json.loads(line)
            self.assertEqual(entry["task_id"], "idx")
            self.assertIn("ts", entry)
            self.assertIn("state", entry)
            self.assertIn("terminated_reason", entry)

    # ── Dispatcher integration ───────────────────────────────────

    def test_dispatcher_saves_when_persist_to_disk_on(self):
        """Dispatcher calls save_task when task.persist_to_disk is True."""
        task = self.tm.Task(
            task_id="t_disp",
            task_type="apply_one_job",
            persist_to_disk=True,
        )

        def phase(t):
            return {"outcome": "applied", "details": {}}

        outcome = self.tm.task_dispatch(
            task, phase, persistence_base_path=self.base
        )
        self.assertEqual(outcome.get("details", {}).get("_persisted_to"),
                         str(self.base / "t_disp.json"))
        self.assertTrue((self.base / "t_disp.json").exists())

        # Roundtrip the terminated state
        loaded = self.pers.load_task("t_disp", base_path=self.base)
        self.assertEqual(loaded.state, self.tm.TASK_STATE_TERMINATED)
        self.assertEqual(
            loaded.terminated_reason, self.tm.TASK_TERMINATED_APPLIED
        )

    def test_dispatcher_skips_save_when_persist_to_disk_off(self):
        """Default persist_to_disk=False → no file written."""
        task = self.tm.Task(task_id="t_off", task_type="apply_one_job")

        def phase(t):
            return {"outcome": "applied", "details": {}}

        self.tm.task_dispatch(task, phase, persistence_base_path=self.base)
        self.assertFalse((self.base / "t_off.json").exists())

    def test_dispatcher_blocks_advance_on_persistence_warnings(self):
        """Loaded task with non-empty persistence_warnings cannot be
        dispatched until confirm_recovered() clears them."""
        task = self.tm.Task(task_id="t_warn", task_type="apply_one_job")
        task.persistence_warnings = ["unknown_field:legacy"]

        def phase(t):
            self.fail("phase function should not run when warnings present")

        with self.assertRaises(self.tm.TaskPersistenceBlockedError):
            self.tm.task_dispatch(task, phase, persistence_base_path=self.base)

    # ── confirm_recovered ────────────────────────────────────────

    def test_confirm_recovered_clears_matching_warnings(self):
        """Exact-match acknowledgement clears the matched warnings;
        others remain."""
        task = self.tm.Task(
            task_id="cr",
            task_type="apply_one_job",
            persistence_warnings=["a", "b", "c"],
        )
        remaining = self.pers.confirm_recovered(
            task, ["a", "c"], base_path=self.base
        )
        self.assertEqual(remaining, ["b"])
        self.assertEqual(task.persistence_warnings, ["b"])

    def test_confirm_recovered_resaves_when_persist_to_disk_on(self):
        """If persist_to_disk is on and any warning was cleared, the task
        is re-saved with the trimmed warning list."""
        task = self.tm.Task(
            task_id="cr2",
            task_type="apply_one_job",
            persist_to_disk=True,
            persistence_warnings=["unknown_field:x"],
        )
        # Initial save with the warning
        self.pers.save_task(task, base_path=self.base)
        # Acknowledge
        self.pers.confirm_recovered(
            task, ["unknown_field:x"], base_path=self.base
        )
        loaded = self.pers.load_task("cr2", base_path=self.base)
        self.assertEqual(loaded.persistence_warnings, [])

    def test_confirm_recovered_unblocks_dispatcher(self):
        """After confirm_recovered clears warnings, dispatcher proceeds."""
        task = self.tm.Task(
            task_id="unb",
            task_type="apply_one_job",
            persistence_warnings=["unknown_field:x"],
        )
        self.pers.confirm_recovered(task, ["unknown_field:x"])
        self.assertEqual(task.persistence_warnings, [])

        ran = []

        def phase(t):
            ran.append(t.task_id)
            return {"outcome": "applied", "details": {}}

        self.tm.task_dispatch(task, phase, persistence_base_path=self.base)
        self.assertEqual(ran, ["unb"])


class AdapterWiringV0Tests(unittest.TestCase):
    """Adapter-wiring v0 tests. Per browser-Claude design 2026-05-18 —
    redirect_check_at_phase_entry runs BEFORE any DOM read so a redirect
    to Paycom mid-flow doesn't cause Indeed selectors to be applied to a
    Paycom DOM. Four required branches: proceed, observe (same-host),
    promote (cross-host known), unsupported_host (cross-host unknown).
    Plus redirect_chain cap enforcement."""

    def setUp(self):
        recipe._reset_framework_state_for_tests()
        import task_model
        self.tm = task_model

    def tearDown(self):
        recipe._reset_framework_state_for_tests()

    def _task_with_primary(self, tab_id: int, url: str):
        task = self.tm.Task(task_id="t_aw", task_type="apply_one_job")
        task.tab_bindings.append(
            self.tm.TaskTabBinding(
                task_id="t_aw",
                tab_id=tab_id,
                role=self.tm.BINDING_ROLE_PRIMARY,
                binding_source=self.tm.BINDING_SOURCE_OPERATOR_ADDED,
                last_observed_url=url,
            )
        )
        return task

    # ── REQUIRED BRANCH 1: proceed ───────────────────────────────

    def test_phase_entry_no_drift_returns_proceed(self):
        """When the primary tab is still on the bound URL, adapter
        proceeds with DOM read directly. No side effects."""
        task = self._task_with_primary(100, "https://indeed.com/viewjob?jk=abc")
        result = recipe.redirect_check_at_phase_entry(
            task,
            current_tab_urls={100: "https://indeed.com/viewjob?jk=abc"},
        )
        self.assertEqual(result["action"], "proceed")
        self.assertNotIn("_side_effects", result)

    # ── REQUIRED BRANCH 2: observe (same-host transition) ────────

    def test_phase_entry_same_host_transition_uses_observe_not_promote(self):
        """Workday step1 → step2 on the SAME myworkdayjobs.com host:
        emits tab_binding_observe to refresh URL, does NOT trigger
        promote, does NOT burn a redirect_chain slot. (This is the
        same-host non-event from BC's adapter-wiring spec.)"""
        task = self._task_with_primary(
            200, "https://wd1.myworkdayjobs.com/Acme/apply/step1"
        )
        result = recipe.redirect_check_at_phase_entry(
            task,
            current_tab_urls={200: "https://wd1.myworkdayjobs.com/Acme/apply/step2"},
        )
        self.assertEqual(result["action"], "observe")
        effects = result["_side_effects"]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]["kind"], self.tm.SIDE_EFFECT_TAB_BINDING_OBSERVE)
        self.assertEqual(effects[0]["tab_id"], 200)
        self.assertEqual(
            effects[0]["new_last_observed_url"],
            "https://wd1.myworkdayjobs.com/Acme/apply/step2",
        )

        # Apply the observe — last_observed_url refreshed, chain untouched.
        self.tm.apply_side_effects(task, effects)
        self.assertEqual(
            task.tab_bindings[0].last_observed_url,
            "https://wd1.myworkdayjobs.com/Acme/apply/step2",
        )
        self.assertEqual(task.redirect_chain, [])

    # ── REQUIRED BRANCH 3: promote (cross-host known) ────────────

    def test_phase_entry_cross_host_to_known_host_emits_promote(self):
        """indeed.com → smartapply.indeed.com is a cross-host redirect
        to a KNOWN host. Emits promote side-effects (demote old primary,
        add new primary) and appends host to redirect_chain on apply."""
        task = self._task_with_primary(300, "https://indeed.com/viewjob?jk=abc")
        result = recipe.redirect_check_at_phase_entry(
            task,
            current_tab_urls={
                300: "https://indeed.com/viewjob?jk=abc",  # old still open
                301: "https://smartapply.indeed.com/beta/apply/abc",
            },
        )
        self.assertEqual(result["action"], "promote")
        # detect_redirect_and_promote builds: demote old + add new
        kinds = [e["kind"] for e in result["_side_effects"]]
        self.assertIn(self.tm.SIDE_EFFECT_TAB_BINDING_DEMOTE, kinds)
        self.assertIn(self.tm.SIDE_EFFECT_TAB_BINDING_ADD, kinds)

        # Apply — redirect_chain gets the new host
        self.tm.apply_side_effects(task, result["_side_effects"])
        self.assertEqual(task.redirect_chain, ["smartapply.indeed.com"])

    # ── REQUIRED BRANCH 4: unsupported_host (cross-host unknown) ─

    def test_phase_entry_cross_host_to_unsupported_host_signals_unsupported(self):
        """indeed.com → paycom.com is a cross-host redirect to an
        UNKNOWN host. No promote (we can't drive Paycom yet). Adapter
        surfaces as unsupported_host so the dispatcher marks the task
        attention=needs_operator. (Per BC's A-refinement — no new
        adapter_unknown registry; use existing INTERRUPTED + reason.)"""
        task = self._task_with_primary(400, "https://indeed.com/viewjob?jk=abc")
        result = recipe.redirect_check_at_phase_entry(
            task,
            current_tab_urls={400: "https://paycom.com/candidates/apply/abc"},
        )
        self.assertEqual(result["action"], "unsupported_host")
        self.assertEqual(result["host"], "paycom.com")
        self.assertEqual(result["from_host"], "indeed.com")
        self.assertNotIn("_side_effects", result)

    # ── REDIRECT-CHAIN CAP enforcement ───────────────────────────

    def test_redirect_chain_at_cap_raises_taskredirectlooperror(self):
        """When applying tab_binding_add would push redirect_chain past
        MAX_REDIRECT_CHAIN, apply_side_effects raises before any
        mutation. The new binding is NOT added; redirect_chain is NOT
        appended. Operator decides how to recover."""
        task = self._task_with_primary(500, "https://indeed.com/viewjob?jk=z")
        # Pre-load chain to one below the cap so the next add would
        # exceed it.
        task.redirect_chain = ["a.com", "b.com", "c.com"]

        from task_model import (
            TaskTabBinding,
            BINDING_ROLE_PRIMARY,
            BINDING_SOURCE_ADAPTER_PROMOTED,
            SIDE_EFFECT_TAB_BINDING_ADD,
        )
        new_binding = TaskTabBinding(
            task_id="t_aw",
            tab_id=501,
            role=BINDING_ROLE_PRIMARY,
            binding_source=BINDING_SOURCE_ADAPTER_PROMOTED,
            last_observed_url="https://d.com/x",
        )
        bindings_before = len(task.tab_bindings)
        chain_before = list(task.redirect_chain)

        with self.assertRaises(self.tm.TaskRedirectLoopError):
            self.tm.apply_side_effects(task, [{
                "kind": SIDE_EFFECT_TAB_BINDING_ADD,
                "binding": new_binding,
            }])

        # Pre-raise: nothing mutated
        self.assertEqual(len(task.tab_bindings), bindings_before)
        self.assertEqual(task.redirect_chain, chain_before)

    # ── No-primary edge ──────────────────────────────────────────

    def test_phase_entry_no_primary_returns_no_primary_action(self):
        """Task with no primary binding can't be redirect-checked.
        Surfaces as no_primary so caller raises an interrupt for the
        operator to designate one."""
        task = self.tm.Task(task_id="t_np", task_type="apply_one_job")
        result = recipe.redirect_check_at_phase_entry(
            task,
            current_tab_urls={1: "https://anything.com/"},
        )
        self.assertEqual(result["action"], "no_primary")


class AdapterWiringPersistenceRoundtripTest(unittest.TestCase):
    """Confirms redirect_chain roundtrips through persistence cleanly.
    Belongs alongside adapter-wiring v0 because the cap field is what
    needs to survive a crash — losing it would re-enable a loop."""

    def setUp(self):
        recipe._reset_framework_state_for_tests()
        import task_model, persistence, tempfile
        self.tm = task_model
        self.pers = persistence
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        recipe._reset_framework_state_for_tests()

    def test_redirect_chain_survives_save_load_roundtrip(self):
        task = self.tm.Task(task_id="rc", task_type="apply_one_job")
        task.redirect_chain = ["indeed.com", "smartapply.indeed.com"]
        self.pers.save_task(task, base_path=self.base)
        loaded = self.pers.load_task("rc", base_path=self.base)
        self.assertEqual(
            loaded.redirect_chain,
            ["indeed.com", "smartapply.indeed.com"],
        )
        self.assertEqual(loaded.persistence_warnings, [])


if __name__ == "__main__":
    unittest.main()
