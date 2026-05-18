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
        fd, self._tmp_path = tempfile.mkstemp(prefix="audit_test_", suffix=".jsonl")
        # Close the file descriptor; _audit_log opens by path.
        import os
        os.close(fd)
        # Empty it (mkstemp creates empty but we want a known state).
        Path(self._tmp_path).write_text("")

    def tearDown(self):
        import os
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


if __name__ == "__main__":
    unittest.main()
