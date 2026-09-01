#!/usr/bin/env python3
"""Unit tests for ~/scripts/typed_actions.py (P0.4).

No master_ai import, no Ollama, no shell. Pure schema + parser + risk
classifier tests.

Run: python3 ~/scripts/test_typed_actions.py
Exit: 0 = all green, non-zero = at least one schema/parser/risk failure.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.expanduser("~/scripts"))

import typed_actions as ta  # noqa: E402


class KindAndRiskConstants(unittest.TestCase):
    def test_directive_kinds_are_complete(self):
        # REMEMBER added 2026-05-11 — model self-write to memory.
        # PLAN/DONE/THINK/RUN_SKILL/SEND_EMAIL added 2026-08-23 — the legacy
        # parser (master_ai._RE_DIRECTIVE, master_ai.py:3780) had grown these
        # after this module's initial 2026-05-17 build; typed_actions had
        # drifted out of sync with it until this gap-closing pass.
        self.assertEqual(
            ta.DIRECTIVE_KINDS,
            frozenset({
                "RUN", "RUNTERM", "READ", "CREATE", "EDIT", "REMEMBER",
                "PLAN", "DONE", "THINK", "RUN_SKILL", "SEND_EMAIL",
                "BROWSER_CLICK", "BROWSER_FILL", "BROWSER_FILL_FORM", "BROWSER_UPLOAD_FILE", "BROWSER_SUBMIT",
                "BROWSER_READ", "BROWSER_NAV", "BROWSER_CLOSE_TAB",
                "BROWSER_SCREENSHOT", "BROWSER_WAIT", "BROWSER_SCROLL",
                "BROWSER_DOUBLE_CLICK", "BROWSER_FIND", "BROWSER_EXTRACT_LIST",
                "BROWSER_DRIVE_INSPECT_FOLDER", "BROWSER_READ_PAGE", "BROWSER_READ_PAGE_FULL",
                "BROWSER_OBSERVE", "BROWSER_CDP_MOUSE", "BROWSER_CDP_KEY",
                "BROWSER_TAB_CREATE", "REMOTE_MCP",
            }),
        )

    def test_kind_class_aliases_match(self):
        for name in (
            "RUN", "RUNTERM", "READ", "CREATE", "EDIT", "REMEMBER",
            "PLAN", "DONE", "THINK", "RUN_SKILL", "SEND_EMAIL",
            "BROWSER_CLICK", "BROWSER_FILL", "BROWSER_FILL_FORM", "BROWSER_UPLOAD_FILE", "BROWSER_SUBMIT",
            "BROWSER_READ", "BROWSER_NAV", "BROWSER_CLOSE_TAB",
            "BROWSER_SCREENSHOT", "BROWSER_WAIT", "BROWSER_SCROLL",
            "BROWSER_DOUBLE_CLICK", "BROWSER_FIND", "BROWSER_EXTRACT_LIST",
            "BROWSER_DRIVE_INSPECT_FOLDER", "BROWSER_READ_PAGE", "BROWSER_READ_PAGE_FULL",
            "BROWSER_OBSERVE", "BROWSER_CDP_MOUSE", "BROWSER_CDP_KEY",
            "BROWSER_TAB_CREATE", "REMOTE_MCP",
        ):
            self.assertEqual(getattr(ta.Kind, name), name)

    def test_risk_constants_are_distinct(self):
        s = {ta.Risk.SAFE, ta.Risk.NORMAL, ta.Risk.HIGH, ta.Risk.BLOCKED}
        self.assertEqual(len(s), 4)


class TypedActionShape(unittest.TestCase):
    def test_construct_with_minimum_fields(self):
        a = ta.TypedAction(kind="RUN", target="ls -la")
        self.assertEqual(a.kind, "RUN")
        self.assertEqual(a.target, "ls -la")
        self.assertTrue(a.id)
        self.assertEqual(a.status, ta.Status.PARSED)

    def test_lowercase_kind_is_normalized(self):
        a = ta.TypedAction(kind="run", target="ls")
        self.assertEqual(a.kind, "RUN")

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            ta.TypedAction(kind="DELETE", target="/tmp")

    def test_id_is_uuid(self):
        a = ta.TypedAction(kind="RUN", target="ls")
        import uuid
        # Should not raise
        uuid.UUID(a.id)

    def test_parsed_at_is_iso8601(self):
        a = ta.TypedAction(kind="RUN", target="ls")
        from datetime import datetime
        # Round-trip via fromisoformat (post-3.11 accepts the 'Z' / offset form we emit)
        datetime.fromisoformat(a.parsed_at)

    def test_to_dict_roundtrip(self):
        a = ta.TypedAction(kind="EDIT", target="/tmp/foo.py",
                           edit_old="x = 1", edit_new="x = 2")
        d = a.to_dict()
        b = ta.TypedAction.from_dict(d)
        self.assertEqual(b.kind, a.kind)
        self.assertEqual(b.target, a.target)
        self.assertEqual(b.edit_old, a.edit_old)
        self.assertEqual(b.edit_new, a.edit_new)

    def test_from_dict_rejects_missing_fields(self):
        with self.assertRaises(ValueError):
            ta.TypedAction.from_dict({"target": "foo"})
        with self.assertRaises(ValueError):
            ta.TypedAction.from_dict({"kind": "RUN"})

    def test_from_dict_rejects_non_dict(self):
        with self.assertRaises(TypeError):
            ta.TypedAction.from_dict("RUN: ls")

    def test_extras_preserved_through_roundtrip(self):
        d = {"kind": "RUN", "target": "ls", "future_field": "x"}
        a = ta.TypedAction.from_dict(d)
        self.assertEqual(a.extras.get("future_field"), "x")

    def test_read_range_roundtrip(self):
        a = ta.TypedAction(kind="READ", target="/tmp/foo.py", read_range=(10, 20))
        d = a.to_dict()
        # Tuples serialize to lists in JSON, dataclass.asdict gives tuple as list.
        # from_dict must restore tuple.
        b = ta.TypedAction.from_dict(d)
        self.assertEqual(b.read_range, (10, 20))


class RiskClassifier(unittest.TestCase):
    def _risk_of(self, kind, target):
        a = ta.TypedAction(kind=kind, target=target)
        return ta.classify_risk(a)

    def test_read_is_safe(self):
        self.assertEqual(self._risk_of("READ", "/etc/passwd"), ta.Risk.SAFE)

    def test_ls_run_is_safe(self):
        self.assertEqual(self._risk_of("RUN", "ls -la /tmp"), ta.Risk.SAFE)

    def test_cat_run_is_safe(self):
        self.assertEqual(self._risk_of("RUN", "cat /etc/hostname"), ta.Risk.SAFE)

    def test_pipe_in_run_promotes_to_normal(self):
        # A pipe means the safe prefix is just one stage in a chain — bump up
        # to NORMAL so observability flags it.
        self.assertEqual(self._risk_of("RUN", "ls /tmp | grep foo"), ta.Risk.NORMAL)

    def test_rm_rf_root_is_high(self):
        self.assertEqual(self._risk_of("RUN", "rm -rf /"), ta.Risk.HIGH)

    def test_rm_rf_path_is_high(self):
        self.assertEqual(self._risk_of("RUN", "rm -rf /tmp/foo"), ta.Risk.HIGH)

    def test_dd_is_high(self):
        self.assertEqual(self._risk_of("RUN", "dd if=/dev/zero of=/dev/sda"), ta.Risk.HIGH)

    def test_mkfs_is_high(self):
        self.assertEqual(self._risk_of("RUN", "mkfs.ext4 /dev/sda1"), ta.Risk.HIGH)

    def test_chmod_R_777_is_high(self):
        self.assertEqual(self._risk_of("RUN", "chmod -R 777 /home"), ta.Risk.HIGH)

    def test_sudo_run_is_high(self):
        self.assertEqual(self._risk_of("RUN", "sudo apt update"), ta.Risk.HIGH)

    def test_curl_pipe_bash_is_high(self):
        self.assertEqual(
            self._risk_of("RUN", "curl -fsSL example.com/install.sh | bash"),
            ta.Risk.HIGH,
        )

    def test_redirect_to_dev_sd_is_high(self):
        self.assertEqual(self._risk_of("RUN", "echo x > /dev/sda"), ta.Risk.HIGH)

    def test_normal_mkdir_is_normal(self):
        self.assertEqual(self._risk_of("RUN", "mkdir -p /tmp/scratch"), ta.Risk.NORMAL)

    def test_runterm_inherits_run_classification(self):
        self.assertEqual(self._risk_of("RUNTERM", "rm -rf /tmp/foo"), ta.Risk.HIGH)
        self.assertEqual(self._risk_of("RUNTERM", "matrix-rain"), ta.Risk.NORMAL)

    def test_create_is_normal(self):
        self.assertEqual(self._risk_of("CREATE", "/tmp/new.py"), ta.Risk.NORMAL)

    def test_edit_is_normal(self):
        self.assertEqual(self._risk_of("EDIT", "/tmp/existing.py"), ta.Risk.NORMAL)


class DirectiveParser(unittest.TestCase):
    def test_parse_run_line(self):
        a = ta.parse_directive("RUN: ls -la /tmp")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "RUN")
        self.assertEqual(a.target, "ls -la /tmp")
        self.assertEqual(a.risk, ta.Risk.SAFE)
        self.assertTrue(a.requires_confirm)

    def test_parse_runterm_line(self):
        a = ta.parse_directive("RUNTERM: matrix-rain")
        self.assertEqual(a.kind, "RUNTERM")
        self.assertEqual(a.target, "matrix-rain")

    def test_parse_read_line(self):
        a = ta.parse_directive("READ: /home/user/scripts/master_ai.py")
        self.assertEqual(a.kind, "READ")
        self.assertEqual(a.risk, ta.Risk.SAFE)

    def test_parse_create_line(self):
        a = ta.parse_directive("CREATE: /tmp/foo.py")
        self.assertEqual(a.kind, "CREATE")

    def test_parse_edit_line(self):
        a = ta.parse_directive("EDIT: /tmp/foo.py")
        self.assertEqual(a.kind, "EDIT")

    def test_parse_browser_action_line(self):
        a = ta.parse_directive("BROWSER_CLICK: button[aria-label='Search']")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "BROWSER_CLICK")
        self.assertEqual(a.target, "button[aria-label='Search']")
        self.assertTrue(a.requires_confirm)

    def test_parse_browser_read_line(self):
        a = ta.parse_directive("BROWSER_READ: main")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "BROWSER_READ")
        self.assertEqual(a.target, "main")

    def test_parse_browser_screenshot_line(self):
        a = ta.parse_directive("BROWSER_SCREENSHOT: viewport")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "BROWSER_SCREENSHOT")

    def test_parse_browser_upload_file_line(self):
        a = ta.parse_directive("BROWSER_UPLOAD_FILE: input[type=file] :: /tmp/resume.pdf")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "BROWSER_UPLOAD_FILE")

    def test_parse_browser_fill_form_line(self):
        a = ta.parse_directive("BROWSER_FILL_FORM: form")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "BROWSER_FILL_FORM")

    def test_parse_browser_submit_line(self):
        a = ta.parse_directive("BROWSER_SUBMIT: form")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "BROWSER_SUBMIT")

    def test_parse_browser_close_tab_line(self):
        a = ta.parse_directive("BROWSER_CLOSE_TAB: current")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "BROWSER_CLOSE_TAB")

    def test_parse_browser_read_page_full_line(self):
        a = ta.parse_directive("BROWSER_READ_PAGE_FULL: current")
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "BROWSER_READ_PAGE_FULL")
        self.assertEqual(a.target, "current")
        self.assertTrue(a.requires_confirm)

    def test_parse_drive_inspect_line(self):
        a = ta.parse_directive(
            'BROWSER_DRIVE_INSPECT_FOLDER: {"query":"resume","variants":["Resume","resume"]}'
        )
        self.assertIsNotNone(a)
        self.assertEqual(a.kind, "BROWSER_DRIVE_INSPECT_FOLDER")
        self.assertIn('"query":"resume"', a.target)
        self.assertEqual(a.risk, ta.Risk.SAFE)

    def test_parse_wait_scroll_find_extract_lines(self):
        cases = {
            "BROWSER_WAIT: 2000": "BROWSER_WAIT",
            "BROWSER_SCROLL: down 800": "BROWSER_SCROLL",
            "BROWSER_FIND: Resume": "BROWSER_FIND",
            "BROWSER_EXTRACT_LIST: drive": "BROWSER_EXTRACT_LIST",
            "BROWSER_DOUBLE_CLICK: [aria-label='Resume']": "BROWSER_DOUBLE_CLICK",
        }
        for line, kind in cases.items():
            with self.subTest(line=line):
                a = ta.parse_directive(line)
                self.assertIsNotNone(a)
                self.assertEqual(a.kind, kind)

    def test_lowercase_keyword_accepted(self):
        a = ta.parse_directive("run: ls")
        self.assertEqual(a.kind, "RUN")

    def test_empty_target_returns_none(self):
        self.assertIsNone(ta.parse_directive("RUN: "))
        self.assertIsNone(ta.parse_directive("RUN:"))

    def test_non_directive_line_returns_none(self):
        self.assertIsNone(ta.parse_directive("Let me run that for you."))
        self.assertIsNone(ta.parse_directive(""))
        self.assertIsNone(ta.parse_directive("# RUN: this is a comment"))

    def test_indentation_tolerated(self):
        a = ta.parse_directive("    RUN: pwd")
        self.assertIsNotNone(a)
        self.assertEqual(a.target, "pwd")

    def test_source_text_preserved(self):
        a = ta.parse_directive("RUN: ls", source_text="RUN: ls")
        self.assertEqual(a.source_text, "RUN: ls")

    def test_model_attribution_preserved(self):
        a = ta.parse_directive("RUN: ls", model="master-ai")
        self.assertEqual(a.created_by_model, "master-ai")


class ReplyParser(unittest.TestCase):
    def test_parse_multiline_reply(self):
        reply = (
            "I'll check the disk first.\n"
            "RUN: df -h\n"
            "Then create a backup:\n"
            "CREATE: /tmp/backup.txt\n"
            "BROWSER_READ: main\n"
            "Then start the service.\n"
            "RUNTERM: htop\n"
        )
        actions = ta.parse_reply(reply, model="master-ai")
        self.assertEqual(len(actions), 4)
        self.assertEqual([a.kind for a in actions], ["RUN", "CREATE", "BROWSER_READ", "RUNTERM"])
        self.assertEqual(actions[0].target, "df -h")

    def test_parse_reply_skips_inline_mentions(self):
        # "Use RUN: ls" mid-prose should NOT parse — the parser requires the
        # directive to start the line (whitespace allowed).
        reply = "You can Use RUN: ls to list files."
        self.assertEqual(ta.parse_reply(reply), [])

    def test_parse_empty_reply(self):
        self.assertEqual(ta.parse_reply(""), [])
        self.assertEqual(ta.parse_reply("\n\n\n"), [])

    def test_parse_non_string_returns_empty(self):
        self.assertEqual(ta.parse_reply(None), [])
        self.assertEqual(ta.parse_reply(123), [])


class AuditOutcomeMapping(unittest.TestCase):
    def test_run_kinds_map_to_run_directive(self):
        for k in ("RUN", "RUN-AUTO", "RUN-ALWAYS"):
            d, s = ta.audit_outcome_from_kind(k)
            self.assertEqual(d, "RUN")
            self.assertEqual(s, ta.Status.COMPLETED)

    def test_block_kinds_map_to_blocked_status(self):
        for k in ("RUN-BLOCK", "RUN-BLOCK-CLEANUP", "RUN-BLOCK-MISSING",
                  "RUNTERM-BLOCK", "POLICY-CMD-BLOCK"):
            _, s = ta.audit_outcome_from_kind(k)
            self.assertEqual(s, ta.Status.BLOCKED, f"{k} not BLOCKED")

    def test_sudo_handoff_is_pending_approval(self):
        d, s = ta.audit_outcome_from_kind("RUN-SUDO-HANDOFF")
        self.assertEqual(s, ta.Status.PENDING_APPROVAL)

    def test_runterm_kinds_map_to_runterm(self):
        for k in ("RUNTERM", "RUNTERM-EMPTY", "RUNTERM-REDIRECT"):
            d, _ = ta.audit_outcome_from_kind(k)
            self.assertEqual(d, "RUNTERM")

    def test_unknown_kind_returns_none(self):
        d, s = ta.audit_outcome_from_kind("MENU-NAVIGATION")
        self.assertIsNone(d)
        self.assertIsNone(s)

    def test_prefix_fallback_for_unmapped_run(self):
        d, s = ta.audit_outcome_from_kind("RUN-NEW-FUTURE-KIND")
        self.assertEqual(d, "RUN")
        self.assertEqual(s, ta.Status.COMPLETED)

    def test_empty_kind(self):
        self.assertEqual(ta.audit_outcome_from_kind(""), (None, None))
        self.assertEqual(ta.audit_outcome_from_kind(None), (None, None))


class MakeAuditRecord(unittest.TestCase):
    def test_make_record_for_run(self):
        r = ta.make_audit_record(
            kind="RUN", detail="ls /tmp",
            profile="elijah", mode="auto", cwd="/home/user",
            model="master-ai",
        )
        self.assertEqual(r["kind"], "RUN")
        self.assertEqual(r["target"], "ls /tmp")
        self.assertEqual(r["status"], ta.Status.COMPLETED)
        self.assertEqual(r["risk"], ta.Risk.SAFE)
        self.assertEqual(r["mode"], "auto")
        self.assertEqual(r["audit_kind"], "RUN")
        self.assertIn("id", r)
        self.assertIn("ts", r)

    def test_make_record_for_blocked(self):
        r = ta.make_audit_record(kind="RUN-BLOCK", detail="rm -rf /")
        self.assertEqual(r["status"], ta.Status.BLOCKED)
        self.assertEqual(r["risk"], ta.Risk.HIGH)

    def test_make_record_for_non_directive_returns_none(self):
        self.assertIsNone(ta.make_audit_record(kind="MENU-OPEN", detail="hub"))

    def test_record_is_json_serializable(self):
        r = ta.make_audit_record(kind="EDIT", detail="/tmp/foo.py")
        json.dumps(r)  # Should not raise

    def test_request_block_returns_none(self):
        # POLICY-REQUEST-BLOCK maps to REQUEST which isn't a directive kind
        # we want in the typed audit log.
        self.assertIsNone(ta.make_audit_record(kind="POLICY-REQUEST-BLOCK",
                                                detail="some request"))


class Serialize(unittest.TestCase):
    def test_serialize_produces_jsonl_line(self):
        a = ta.TypedAction(kind="RUN", target="ls")
        s = ta.serialize(a)
        d = json.loads(s)
        self.assertEqual(d["kind"], "RUN")
        self.assertEqual(d["target"], "ls")
        self.assertNotIn("\n", s, "serialize must emit one line for jsonl")


class ReplyParserWithBodies(unittest.TestCase):
    """Fast, standalone unit tests for parse_reply_with_bodies() — content
    capture and the backtick-parity / REMEMBER-in-body suppression it adds
    on top of parse_reply(). No master_ai import (see module docstring).
    The full legacy-vs-typed parity fixtures live in
    test_typed_dispatch_e2e.py; these are narrower, faster checks of the
    same parser in isolation."""

    def test_create_populates_create_content(self):
        text = "CREATE: /tmp/x.py\n<<<CONTENT\nprint(1)\n>>>CONTENT\n"
        actions = ta.parse_reply_with_bodies(text)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "CREATE")
        self.assertEqual(actions[0].create_content, "print(1)")

    def test_edit_populates_old_and_new(self):
        text = (
            "EDIT: /tmp/x.py\n"
            "<<<FIND\nprint(1)\n>>>FIND\n"
            "<<<REPLACE\nprint(2)\n>>>REPLACE\n"
        )
        actions = ta.parse_reply_with_bodies(text)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "EDIT")
        self.assertEqual(actions[0].edit_old, "print(1)")
        self.assertEqual(actions[0].edit_new, "print(2)")

    def test_non_string_input_returns_empty_list(self):
        self.assertEqual(ta.parse_reply_with_bodies(None), [])
        self.assertEqual(ta.parse_reply_with_bodies(123), [])

    def test_backtick_wrapped_keyword_does_not_fire(self):
        actions = ta.parse_reply_with_bodies("Use `RUN:` to run a command.")
        self.assertEqual(actions, [])

    def test_remember_line_inside_edit_body_does_not_fire(self):
        text = (
            "EDIT: /tmp/x.py\n"
            "<<<FIND\nold\n>>>FIND\n"
            "<<<REPLACE\nREMEMBER: not a real directive here\n>>>REPLACE\n"
        )
        actions = ta.parse_reply_with_bodies(text)
        self.assertEqual([a.kind for a in actions], ["EDIT"])
        self.assertIn("REMEMBER:", actions[0].edit_new)

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(ta.parse_reply_with_bodies(""), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
