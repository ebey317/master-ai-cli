#!/usr/bin/env python3
"""Phase 2 safety contract for Chrome always-confirm browser heuristics.

The production implementation lives in sensei_extension/side_panel.js. These
tests intentionally mirror the stable regex contract in Python so the safety
categories are pinned without needing a browser runtime.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


PURCHASE_RE = re.compile(
    r"\b(buy|purchase|pay|checkout|order|subscribe|add to cart)\b", re.I
)
DELETE_RE = re.compile(
    r"\b(delete|remove|destroy|uninstall|erase|wipe|cancel.*(account|subscription))\b",
    re.I,
)
AUTH_RE = re.compile(
    r"\b(sign[-_\s]*up|sign[-_\s]*in|log[-_\s]*in|log[-_\s]*out|register|authorize|grant\s*access|oauth|api\s*key)\b",
    re.I,
)
SENSITIVE_RE = re.compile(
    r"\b(password|ssn|social.*security|credit.*card|cvv|cvc|api.*key|bank.*account|routing.*number|passport|passport.*number|medical(.*record)?|diagnosis|health.*insurance|patient.*id|driver.*license|driver.*licence|date.*of.*birth|dob)\b",
    re.I,
)
PASSWORD_SEL = re.compile(
    r"type=[\"']?password[\"']?|name=[\"']?(password|pwd|passwd)[\"']?", re.I
)
PURCHASE_URL_RE = re.compile(r"/(checkout|cart|pay|order)\b", re.I)
READONLY_BROWSER_KINDS = {
    "BROWSER_READ",
    "BROWSER_SCREENSHOT",
    "BROWSER_WAIT",
    "BROWSER_SCROLL",
    "BROWSER_FIND",
    "BROWSER_EXTRACT_LIST",
    "BROWSER_DRIVE_INSPECT_FOLDER",
}


def classify_browser_action(action):
    kind = str(action.get("kind") or "").upper()
    target = str(action.get("target") or "").lower()

    if kind == "BROWSER_FILL" and (
        PASSWORD_SEL.search(target) or SENSITIVE_RE.search(target)
    ):
        return {
            "safe": False,
            "requires_confirm": True,
            "gated_by": "irreversible_heuristic:sensitive_fill",
        }
    if kind == "BROWSER_CLICK":
        if PURCHASE_RE.search(target):
            return {
                "safe": False,
                "requires_confirm": True,
                "gated_by": "irreversible_heuristic:purchase",
            }
        if DELETE_RE.search(target):
            return {
                "safe": False,
                "requires_confirm": True,
                "gated_by": "irreversible_heuristic:delete",
            }
        if AUTH_RE.search(target):
            return {
                "safe": False,
                "requires_confirm": True,
                "gated_by": "irreversible_heuristic:auth",
            }
    if kind == "BROWSER_NAV":
        if PURCHASE_RE.search(target) or PURCHASE_URL_RE.search(target):
            return {
                "safe": False,
                "requires_confirm": True,
                "gated_by": "irreversible_heuristic:purchase_url",
            }
    if kind in READONLY_BROWSER_KINDS:
        return {"safe": True, "requires_confirm": False, "gated_by": None}
    return {"safe": True, "requires_confirm": False, "gated_by": None}


class BrowserHeuristicTests(unittest.TestCase):
    def assert_gated(self, action, gated_by):
        actual = classify_browser_action(action)
        self.assertFalse(actual["safe"])
        self.assertTrue(actual["requires_confirm"])
        self.assertEqual(actual["gated_by"], gated_by)

    def assert_safe(self, action):
        actual = classify_browser_action(action)
        self.assertTrue(actual["safe"])
        self.assertFalse(actual["requires_confirm"])
        self.assertIsNone(actual["gated_by"])

    def test_buy_button_is_purchase_gated(self):
        self.assert_gated(
            {"kind": "BROWSER_CLICK", "target": "button[aria-label='Buy now']"},
            "irreversible_heuristic:purchase",
        )

    def test_delete_account_link_is_delete_gated(self):
        self.assert_gated(
            {"kind": "BROWSER_CLICK", "target": "a.delete-account"},
            "irreversible_heuristic:delete",
        )

    def test_sign_up_button_is_auth_gated(self):
        self.assert_gated(
            {"kind": "BROWSER_CLICK", "target": "button#sign-up"},
            "irreversible_heuristic:auth",
        )

    def test_password_fill_is_sensitive_gated(self):
        self.assert_gated(
            {"kind": "BROWSER_FILL", "target": "input[type='password']"},
            "irreversible_heuristic:sensitive_fill",
        )

    def test_passport_fill_is_sensitive_gated(self):
        # Anthropic-spec hard-limit category — passport numbers.
        self.assert_gated(
            {"kind": "BROWSER_FILL", "target": "input[name='passport-number']"},
            "irreversible_heuristic:sensitive_fill",
        )

    def test_medical_record_fill_is_sensitive_gated(self):
        # Anthropic-spec hard-limit category — medical data.
        self.assert_gated(
            {
                "kind": "BROWSER_FILL",
                "target": "input[aria-label='medical record number']",
            },
            "irreversible_heuristic:sensitive_fill",
        )

    def test_diagnosis_fill_is_sensitive_gated(self):
        self.assert_gated(
            {"kind": "BROWSER_FILL", "target": "textarea[name='diagnosis-notes']"},
            "irreversible_heuristic:sensitive_fill",
        )

    def test_health_insurance_fill_is_sensitive_gated(self):
        self.assert_gated(
            {
                "kind": "BROWSER_FILL",
                "target": "input[aria-label='Health Insurance ID']",
            },
            "irreversible_heuristic:sensitive_fill",
        )

    def test_drivers_license_fill_is_sensitive_gated(self):
        self.assert_gated(
            {"kind": "BROWSER_FILL", "target": "input[name='drivers-license']"},
            "irreversible_heuristic:sensitive_fill",
        )

    def test_dob_fill_is_sensitive_gated(self):
        # Date of birth — birth-class identifier per Anthropic's PII guidance.
        self.assert_gated(
            {"kind": "BROWSER_FILL", "target": "input[name='dob']"},
            "irreversible_heuristic:sensitive_fill",
        )

    def test_date_of_birth_phrase_fill_is_sensitive_gated(self):
        self.assert_gated(
            {"kind": "BROWSER_FILL", "target": "input[aria-label='Date of Birth']"},
            "irreversible_heuristic:sensitive_fill",
        )

    def test_checkout_url_is_purchase_url_gated(self):
        self.assert_gated(
            {"kind": "BROWSER_NAV", "target": "https://shop.example.com/checkout"},
            "irreversible_heuristic:purchase_url",
        )

    def test_read_is_safe(self):
        self.assert_safe({"kind": "BROWSER_READ", "target": "main"})

    def test_screenshot_is_safe(self):
        self.assert_safe({"kind": "BROWSER_SCREENSHOT", "target": "viewport"})

    def test_drive_inspect_is_safe(self):
        self.assert_safe(
            {
                "kind": "BROWSER_DRIVE_INSPECT_FOLDER",
                "target": '{"query":"resume"}',
            }
        )

    def test_read_more_click_is_safe(self):
        self.assert_safe({"kind": "BROWSER_CLICK", "target": "a.read-more"})

    def test_search_fill_is_safe(self):
        self.assert_safe({"kind": "BROWSER_FILL", "target": "input#search-box"})


class ContinuationFormattingTests(unittest.TestCase):
    def test_format_action_results_includes_gated_by(self):
        from stt_server import _format_action_results  # noqa: WPS433

        text = _format_action_results(
            [
                {
                    "action": {
                        "kind": "BROWSER_CLICK",
                        "target": "button[aria-label='Buy now']",
                    },
                    "verdict": "accept",
                    "result": "success",
                    "gated_by": "irreversible_heuristic:purchase",
                }
            ]
        )
        self.assertIn("gated_by: irreversible_heuristic:purchase", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
