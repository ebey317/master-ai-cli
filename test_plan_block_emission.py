#!/usr/bin/env python3
"""Regression test for the PLAN-as-block model behavior (Phase 5 of the
Anthropic Claude-for-Chrome process spec, mirrored at
~/MD/reference_anthropic_claude_for_chrome_process.md).

When the user asks for a task that needs 3+ browser actions, the model
should open its reply with a <PLAN>…</PLAN> block listing the sites
and steps, then emit the BROWSER_* directives after </PLAN>. The
Chrome extension uses the block to render one Approve-All card
instead of N per-action approves.

Asserts model behavior, not extension UI:
  - reply text contains <PLAN> … </PLAN>
  - block contains a Sites line and a Steps section
  - block contains an Irreversible line (none or one-line description)
  - BROWSER_* directives appear AFTER </PLAN>
  - actions[] still parses out the directives (back-compat with the
    existing per-action audit + dispatch path)

For single-step asks (one click, one fill, one screenshot, one read),
the PLAN block must NOT appear — see test_no_plan_for_single_step.

Run: python3 ~/scripts/test_plan_block_emission.py
"""
import json
import re
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8080"
TIMEOUT_S = 20
CHAT_TIMEOUT_S = 600
FIXTURE_URL = "file:///home/user/scripts/sensei_extension/test/job_app_smoke.html"

MULTI_STEP_INTERACTIVE = "\n".join([
    '1. textbox "First name" selector=#firstName',
    '2. textbox "Last name" selector=#lastName',
    '3. textbox "Email" selector=#email',
    '4. textbox "Phone" selector=#phone',
    '5. textbox "City" selector=#city',
    '6. combobox "State" selector=#state',
    '7. textbox "ZIP code" selector=#zip',
    '8. spinbutton "Years of experience" selector=#yearsExperience',
    '9. radio "Yes" selector=input[name="workAuth"][value="yes"]',
    '10. textbox "Cover letter" selector=#coverLetter',
    '11. button "Submit application" selector=#submitButton',
])

SINGLE_STEP_INTERACTIVE = "\n".join([
    '1. button "Submit" selector=#submitButton',
])


def _read_token():
    return (Path.home() / ".master_ai_extension_token").read_text().strip()


def _post(path, body, *, timeout=TIMEOUT_S, token=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={
            "Content-Type": "application/json",
            **({"X-Master-AI-Token": token} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        return e.code, (json.loads(text) if text else {})


def _chat(prompt, *, interactive_elements, token):
    body = {
        "prompt": prompt,
        "mode": "auto",
        "source": "chrome_extension",
        "session_id": f"plan-block-test-{int(time.time())}",
        "page_context": {
            "url": FIXTURE_URL,
            "title": "Sensei Job App Smoke",
            "interactive_elements": interactive_elements,
        },
    }
    return _post("/chat", body, timeout=CHAT_TIMEOUT_S, token=token)


class PlanBlockEmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            urllib.request.urlopen(BASE + "/health", timeout=2).read()
        except Exception as e:
            raise unittest.SkipTest(f"stt_server not reachable: {e}")
        cls.token = _read_token()

    def test_multi_step_emits_plan_block_with_required_sections(self):
        prompt = (
            "Fill this job application for Elijah W., 317-555-0100, "
            "you@example.com, Indianapolis IN 46201, 10 years "
            'experience, authorized to work in the US, cover letter '
            '"I want this job", then submit.'
        )
        status, body = _chat(
            prompt, interactive_elements=MULTI_STEP_INTERACTIVE, token=self.token
        )
        self.assertEqual(status, 200, f"/chat returned {status}: {body}")
        reply = body.get("reply") or ""

        print("\n=== REPLY (first 1200 chars) ===")
        print(reply[:1200])

        # Block presence.
        m_open = reply.find("<PLAN>")
        m_close = reply.find("</PLAN>")
        self.assertGreaterEqual(m_open, 0,
                                "model did not emit <PLAN> opening tag for multi-step browser task")
        self.assertGreater(m_close, m_open,
                           "</PLAN> tag missing or before <PLAN>")
        block = reply[m_open + len("<PLAN>"): m_close]

        # Required section labels.
        self.assertRegex(block, r"(?im)^\s*Sites:",
                         "PLAN block missing 'Sites:' line")
        self.assertRegex(block, r"(?im)^\s*Steps:",
                         "PLAN block missing 'Steps:' section")
        self.assertRegex(block, r"(?im)^\s*Irreversible:",
                         "PLAN block missing 'Irreversible:' line")

        # Steps should be numbered (1., 2., …); at least 3 for multi-step.
        step_lines = re.findall(r"(?m)^\s*\d+\.\s+.+", block)
        self.assertGreaterEqual(len(step_lines), 3,
                                f"PLAN block lists only {len(step_lines)} steps; "
                                "multi-step asks should enumerate 3+")

        # BROWSER_* directives must appear AFTER </PLAN>, not inside the block.
        tail = reply[m_close + len("</PLAN>"):]
        self.assertRegex(tail, r"BROWSER_(FILL|CLICK|NAV)\s*:",
                         "no BROWSER_* directives after </PLAN>; the block is "
                         "decorative without execution")
        # Directives must NOT also be inside the PLAN block (otherwise the
        # extension would dispatch them as if they were render hints).
        self.assertNotRegex(block, r"^BROWSER_(FILL|CLICK|NAV|READ|SCREENSHOT)\s*:",
                            "BROWSER_* directive appears INSIDE the PLAN block; "
                            "those should be after </PLAN>")

        # actions[] still parses out the executable directives.
        kinds = [str(a.get("kind") or "").upper() for a in (body.get("actions") or [])]
        self.assertIn("BROWSER_FILL", kinds,
                      "PLAN block emitted but no BROWSER_FILL action parsed")
        self.assertIn("BROWSER_CLICK", kinds,
                      "PLAN block emitted but no BROWSER_CLICK action parsed")

    def test_single_step_does_not_emit_plan_block(self):
        # One-action ask should skip the block and emit the directive directly.
        prompt = "click the Submit button"
        status, body = _chat(
            prompt, interactive_elements=SINGLE_STEP_INTERACTIVE, token=self.token
        )
        self.assertEqual(status, 200, f"/chat returned {status}: {body}")
        reply = body.get("reply") or ""

        print("\n=== SINGLE-STEP REPLY (first 400 chars) ===")
        print(reply[:400])

        self.assertNotIn("<PLAN>", reply,
                         "model emitted <PLAN> block for a single-step ask; "
                         "block is only for 3+ actions per Modelfile contract")


if __name__ == "__main__":
    unittest.main(verbosity=2)
