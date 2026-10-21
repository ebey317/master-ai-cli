#!/usr/bin/env python3
"""Regression tests for leaked <arg_key>/<arg_value> XML in directive payloads.

Reproduced live 2026-09-14 and re-confirmed 2026-10-21 against the
current dispatch path: a small model wraps a directive's ENTIRE payload
in its own native tool-call XML tags.

    RUN: <arg_value>ls foo</arg_value>

The old parser truncated the payload at the first tag it found, which
was correct only when the tag trailed AFTER real content. Truncating
"everything before the first tag" wiped the wrapped payload to "" -- so
the directive silently vanished. `has_directives` was already True (RUN
has a colon), which meant the malformed-directive repair detector -- the
safety net for exactly this class of botched output -- never fired
either. Nothing ran, nothing errored, nothing told the model to fix it.

These tests pin the resolution of the payload, not just the helper, so
the silent-vanish failure mode cannot come back.
"""

import os
import sys
import unittest

os.environ["SENSEI_TUI"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dispatch  # noqa: E402
import master_ai  # noqa: E402
from directive_xml_noise import strip_arg_xml_noise  # noqa: E402


class StripArgXmlNoiseTests(unittest.TestCase):
    """Unit coverage of the shared helper."""

    def test_trailing_noise_keeps_content_before_the_tag(self):
        # The shape that already worked: real content first, tags after.
        self.assertEqual(
            strip_arg_xml_noise("ls foo</arg_value><arg_key>description</arg_key>"),
            "ls foo",
        )

    def test_wrapped_payload_is_unwrapped(self):
        # The bug: the whole payload is inside the tags.
        self.assertEqual(
            strip_arg_xml_noise("<arg_value>ls foo</arg_value>"),
            "ls foo",
        )

    def test_key_value_pair_takes_the_value_not_the_key(self):
        # <arg_key> names the PARAMETER ("command"); <arg_value> holds the
        # payload. Taking the key would run the literal word "command".
        self.assertEqual(
            strip_arg_xml_noise(
                "<arg_key>command</arg_key><arg_value>ls foo</arg_value>"
            ),
            "ls foo",
        )

    def test_value_first_pair_also_takes_the_value(self):
        self.assertEqual(
            strip_arg_xml_noise(
                "<arg_value>ls foo</arg_value><arg_key>command</arg_key>"
            ),
            "ls foo",
        )

    def test_clean_payload_is_untouched(self):
        self.assertEqual(strip_arg_xml_noise("ls foo"), "ls foo")
        self.assertEqual(strip_arg_xml_noise(""), "")

    def test_wrapped_but_empty_yields_empty(self):
        # Nothing to act on -- better empty than the raw tag text.
        self.assertEqual(strip_arg_xml_noise("<arg_value></arg_value>"), "")

    def test_unclosed_tag_keeps_content_after_it(self):
        self.assertEqual(strip_arg_xml_noise("<arg_value>ls foo"), "ls foo")

    def test_whitespace_and_attribute_variants(self):
        self.assertEqual(
            strip_arg_xml_noise("< arg_value >ls foo</ arg_value >"), "ls foo"
        )
        self.assertEqual(
            strip_arg_xml_noise('<arg_value name="x">ls foo</arg_value>'), "ls foo"
        )


class DirectiveDispatchTests(unittest.TestCase):
    """End-to-end coverage through the real dispatch path.

    The failure mode being pinned is SILENT -- a directive that produces
    zero executions with no error. So these assert on what actually
    reached the executor, not on a return value.
    """

    def setUp(self):
        self.runs = []
        self.searches = []
        self._saved = {
            name: getattr(master_ai, name)
            for name in (
                "confirm_run",
                "web_search",
                "_action_ok",
                "log",
                "render_reply",
                "_is_noop_cmd",
            )
        }

        class _Result:
            exit_code = 0
            output = "ok"

            def __bool__(self):
                return True

        master_ai.confirm_run = lambda cmd, *a, **k: (
            self.runs.append(cmd),
            _Result(),
        )[1]
        master_ai.web_search = lambda q, *a, **k: (
            self.searches.append(q),
            _Result(),
        )[1]
        master_ai._action_ok = lambda r: True
        master_ai.log = lambda *a, **k: None
        master_ai.render_reply = lambda *a, **k: None
        master_ai._is_noop_cmd = lambda s: False

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(master_ai, name, value)

    def _run_reply(self, reply):
        dispatch.process_reply(reply, [], streamed=False)

    def test_wrapped_run_directive_executes(self):
        # The core regression: this used to execute NOTHING.
        self._run_reply("RUN: <arg_value>ls foo</arg_value>")
        self.assertEqual(self.runs, ["ls foo"])

    def test_key_value_pair_run_directive_executes(self):
        self._run_reply("RUN: <arg_key>command</arg_key><arg_value>ls foo</arg_value>")
        self.assertEqual(self.runs, ["ls foo"])

    def test_trailing_noise_run_directive_still_executes(self):
        # The shape that already worked -- must not regress.
        self._run_reply("RUN: ls foo</arg_value><arg_key>description</arg_key>")
        self.assertEqual(self.runs, ["ls foo"])

    def test_plain_run_directive_still_executes(self):
        self._run_reply("RUN: ls foo")
        self.assertEqual(self.runs, ["ls foo"])

    def test_wrapped_search_directive_executes(self):
        # SEARCH hit a SECOND root cause: has_directives omitted
        # search_queries, so the malformed-directive detector fired and
        # forced a retry instead of running the search.
        self._run_reply("SEARCH: <arg_value>forecast</arg_value>")
        self.assertEqual(self.searches, ["forecast"])

    def test_key_value_pair_search_directive_executes(self):
        self._run_reply(
            "SEARCH: <arg_key>query</arg_key><arg_value>forecast</arg_value>"
        )
        self.assertEqual(self.searches, ["forecast"])

    def test_empty_wrapped_payload_executes_nothing(self):
        # Nothing usable inside the tags -> nothing to run. Not a crash,
        # not a wrong command.
        self._run_reply("RUN: <arg_value></arg_value>")
        self.assertEqual(self.runs, [])

    def test_repair_detector_still_fires_for_genuinely_malformed_output(self):
        # The safety net must NOT be disabled by the fix: a botched
        # tool-call dump with no real directive should still be turned
        # into a repair request rather than silently accepted.
        history = []
        dispatch.process_reply(
            "Let me check that file.\nRUN find /tmp/x <arg_key>cmd</arg_key>",
            history,
            streamed=False,
        )
        self.assertTrue(history, "expected a repair message to be fed back")
        self.assertIn("repair", str(history[-1].get("content", "")).lower())


if __name__ == "__main__":
    unittest.main()
