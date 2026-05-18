"""Unit tests for directive_validator.validate_directive and apply_reject.

Mirrors test_master_ai_parser.py's style — no shell execution, in-process
only. Covers each in-scope kind (READ, RUN, RUNTERM, SEND_EMAIL, CREATE,
EDIT) with valid + invalid fixtures, plus the [VALIDATOR REJECT: ...]
history-line format check and the audit-row schema.

Out-of-scope kinds (BROWSER_*, RUN_SKILL, REMEMBER, DONE, ASK) pass
through; one test asserts that explicitly so the boundary doesn't drift.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from directive_validator import (
    apply_reject,
    reject_message,
    validate_directive,
    write_validator_reject_audit,
    _VALIDATOR_AUDIT_PATH,
)


class RunValidatorTests(unittest.TestCase):
    def test_empty_target_rejected(self):
        ok, reason = validate_directive("RUN", "")
        self.assertFalse(ok)
        self.assertIn("empty", reason.lower())

    def test_whitespace_only_target_rejected(self):
        ok, reason = validate_directive("RUN", "   \t  ")
        self.assertFalse(ok)

    def test_naked_operator_rejected(self):
        for op in (";", "|", "&", "&&", "||", ":"):
            ok, reason = validate_directive("RUN", op)
            self.assertFalse(ok, f"naked {op!r} should reject")
            self.assertIn("operator", reason.lower())

    def test_real_command_valid(self):
        ok, reason = validate_directive("RUN", "ls /tmp")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_runterm_same_rules(self):
        self.assertFalse(validate_directive("RUNTERM", "")[0])
        self.assertTrue(validate_directive("RUNTERM", "top")[0])
        self.assertFalse(validate_directive("RUNTERM", "|")[0])

    def test_kind_normalization(self):
        # Case-insensitive kind.
        self.assertTrue(validate_directive("run", "ls")[0])
        self.assertFalse(validate_directive("run", "")[0])


class ReadValidatorTests(unittest.TestCase):
    def test_empty_path_rejected(self):
        ok, reason = validate_directive("READ", "")
        self.assertFalse(ok)

    def test_traversal_rejected(self):
        ok, reason = validate_directive("READ", "../../etc/passwd")
        self.assertFalse(ok)
        self.assertIn("traversal", reason.lower())

    def test_normpath_collapses_benign_mid_traversal(self):
        # Absolute path with mid-segment `..` normalizes away (`/home/elijah/../etc/passwd`
        # → `/home/etc/passwd`). The regex looks for `..` SEGMENTS that survive
        # normpath, so this passes validator. Path-policy enforcement on the
        # resolved path remains the responsibility of the existing read_fence
        # guard at master_ai.py:9556 — validator's job is only directive shape.
        ok, _ = validate_directive("READ", "/home/elijah/../etc/passwd")
        self.assertTrue(ok)

    def test_valid_absolute_path(self):
        ok, reason = validate_directive("READ", "/etc/hosts")
        self.assertTrue(ok)

    def test_valid_home_expansion(self):
        ok, reason = validate_directive("READ", "~/Documents/notes.md")
        self.assertTrue(ok)

    def test_valid_relative_path_no_traversal(self):
        ok, reason = validate_directive("READ", "scripts/master_ai.py")
        self.assertTrue(ok)


class SendEmailValidatorTests(unittest.TestCase):
    def test_empty_payload_rejected(self):
        ok, _ = validate_directive("SEND_EMAIL", "")
        self.assertFalse(ok)

    def test_missing_to_rejected(self):
        ok, reason = validate_directive("SEND_EMAIL", 'subject="hi" body="yo"')
        self.assertFalse(ok)
        self.assertIn("to=", reason)

    def test_missing_subject_rejected(self):
        ok, reason = validate_directive("SEND_EMAIL", "to=x@y.com body=yo")
        self.assertFalse(ok)
        self.assertIn("subject=", reason)

    def test_to_and_subject_valid(self):
        ok, reason = validate_directive("SEND_EMAIL", 'to=x@y.com subject="Hello"')
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_quoted_subject_valid(self):
        ok, _ = validate_directive(
            "SEND_EMAIL", "to=x@y.com subject='Hi there' body='Sup'"
        )
        self.assertTrue(ok)


class CreateValidatorTests(unittest.TestCase):
    def test_empty_filepath_rejected(self):
        ok, _ = validate_directive("CREATE", "", body="content")
        self.assertFalse(ok)

    def test_none_body_rejected(self):
        ok, reason = validate_directive("CREATE", "/tmp/x.txt", body=None)
        self.assertFalse(ok)
        self.assertIn("CONTENT", reason)

    def test_empty_body_rejected(self):
        ok, _ = validate_directive("CREATE", "/tmp/x.txt", body="")
        self.assertFalse(ok)

    def test_whitespace_body_rejected(self):
        ok, _ = validate_directive("CREATE", "/tmp/x.txt", body="   \n  ")
        self.assertFalse(ok)

    def test_valid_create(self):
        ok, reason = validate_directive(
            "CREATE", "/tmp/x.txt", body="hello world\n"
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")


class EditValidatorTests(unittest.TestCase):
    def test_empty_filepath_rejected(self):
        ok, _ = validate_directive(
            "EDIT", "", body={"find": "x", "replace": "y"}
        )
        self.assertFalse(ok)

    def test_none_body_rejected(self):
        ok, reason = validate_directive("EDIT", "/tmp/x.txt", body=None)
        self.assertFalse(ok)
        self.assertIn("FIND", reason)

    def test_missing_find_rejected(self):
        ok, reason = validate_directive(
            "EDIT", "/tmp/x.txt", body={"replace": "y"}
        )
        self.assertFalse(ok)
        self.assertIn("FIND", reason)

    def test_missing_replace_rejected(self):
        ok, reason = validate_directive(
            "EDIT", "/tmp/x.txt", body={"find": "x"}
        )
        self.assertFalse(ok)
        self.assertIn("REPLACE", reason)

    def test_empty_replace_allowed_as_delete(self):
        ok, _ = validate_directive(
            "EDIT", "/tmp/x.txt", body={"find": "old", "replace": ""}
        )
        self.assertTrue(ok)

    def test_valid_edit(self):
        ok, _ = validate_directive(
            "EDIT", "/tmp/x.txt", body={"find": "old", "replace": "new"}
        )
        self.assertTrue(ok)


class OutOfScopePassThroughTests(unittest.TestCase):
    """Kinds the plan named OUT OF SCOPE must pass through cleanly."""

    def test_browser_click_passes(self):
        self.assertEqual(validate_directive("BROWSER_CLICK", "button"), (True, ""))

    def test_browser_fill_passes(self):
        self.assertEqual(validate_directive("BROWSER_FILL", "#x :: y"), (True, ""))

    def test_browser_nav_passes(self):
        self.assertEqual(validate_directive("BROWSER_NAV", "https://x.com"), (True, ""))

    def test_remember_passes(self):
        self.assertEqual(validate_directive("REMEMBER", "fact"), (True, ""))

    def test_done_passes(self):
        self.assertEqual(validate_directive("DONE", ""), (True, ""))

    def test_ask_passes(self):
        self.assertEqual(validate_directive("ASK", ""), (True, ""))

    def test_run_skill_passes(self):
        self.assertEqual(
            validate_directive("RUN_SKILL", "apply-job-session {}"), (True, "")
        )


class RejectMessageFormatTests(unittest.TestCase):
    def test_em_dash_separator(self):
        msg = reject_message("RUN", "empty command")
        self.assertEqual(msg, "[VALIDATOR REJECT: RUN — empty command]")

    def test_kind_uppercased(self):
        msg = reject_message("run", "x")
        self.assertIn("RUN —", msg)

    def test_includes_prefix_and_brackets(self):
        msg = reject_message("READ", "path traversal")
        self.assertTrue(msg.startswith("[VALIDATOR REJECT:"))
        self.assertTrue(msg.endswith("]"))


class ApplyRejectTests(unittest.TestCase):
    def test_appends_user_role_line(self):
        history = []
        apply_reject(history, "RUN", "empty command")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["role"], "user")
        self.assertIn("VALIDATOR REJECT", history[0]["content"])
        self.assertIn("RUN —", history[0]["content"])

    def test_does_not_clobber_existing_history(self):
        history = [{"role": "system", "content": "init"}]
        apply_reject(history, "READ", "path traversal")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "system")


class AuditRowTests(unittest.TestCase):
    def setUp(self):
        # Redirect audit writes to a temp file so the real audit log
        # isn't polluted by tests.
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        )
        self.tmp.close()
        import directive_validator as dv
        self._orig_path = dv._VALIDATOR_AUDIT_PATH
        dv._VALIDATOR_AUDIT_PATH = self.tmp.name

    def tearDown(self):
        import directive_validator as dv
        dv._VALIDATOR_AUDIT_PATH = self._orig_path
        os.unlink(self.tmp.name)

    def test_audit_row_schema(self):
        write_validator_reject_audit(
            "RUN",
            "empty command",
            request_id="req-123",
            source="api_handle",
        )
        with open(self.tmp.name) as fh:
            rows = [json.loads(l) for l in fh.read().splitlines()]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["kind"], "validator_reject")
        self.assertEqual(r["directive_kind"], "RUN")
        self.assertEqual(r["reason"], "empty command")
        self.assertEqual(r["request_id"], "req-123")
        self.assertEqual(r["source"], "api_handle")
        self.assertIn("ts", r)

    def test_target_preview_capped_at_80(self):
        write_validator_reject_audit(
            "READ", "x", target_preview="a" * 200
        )
        with open(self.tmp.name) as fh:
            row = json.loads(fh.read().strip())
        self.assertEqual(len(row["target_preview"]), 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
