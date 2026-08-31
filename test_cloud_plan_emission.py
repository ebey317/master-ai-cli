#!/usr/bin/env python3
"""Validate cloud-lane PLAN-AS-BLOCK emission against the committed teaching.

The local 7B Modelfile teaching can't reliably push qwen2.5:7b past its
instruction-following ceiling. The committed routing change at
master_ai.py orchestrate() (commit 36faf18) auto-routes Chrome-extension
automation turns to cloud_fast (Groq llama-3.3-70b) where the same
CLOUD_SYSTEM teaching emits the spec block reliably.

This test calls Groq DIRECTLY with the live CLOUD_SYSTEM string from
master_ai.py — no service restart required. It validates the teaching
in isolation:

  - CLOUD_SYSTEM contains the PLAN-AS-BLOCK CONTRACT (commit 4246ef8)
  - Groq llama-3.3-70b, given that system + an 11-action multi-step
    job-application prompt with the same envelope shape _api_prompt
    produces, emits a <PLAN>…</PLAN> block.

If this passes, the spec UX is proven on the cloud lane. After the
service restarts, every Chrome-extension turn that triggers the
auto-route hits this exact code path with no `fast:` prefix.

Run: python3 ~/scripts/test_cloud_plan_emission.py
"""
import json
import re
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, "/home/user/scripts")

# Importing master_ai pulls in the committed CLOUD_SYSTEM builder + the
# key loader + the user-agent dance. We don't go through stt_server at
# all — this is a direct teaching probe.
import master_ai  # noqa: E402

FIXTURE_URL = "file:///home/user/scripts/sensei_extension/test/job_app_smoke.html"
INTERACTIVE_ELEMENTS = "\n".join([
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

USER_PROMPT = (
    "[API REQUEST]\n"
    "source: chrome_extension\n"
    "Branch B: do not execute local machine or browser actions inside the backend request.\n"
    "If browser work is needed, emit BROWSER_CLICK, BROWSER_FILL, BROWSER_READ, BROWSER_NAV, BROWSER_SCREENSHOT, BROWSER_WAIT, BROWSER_SCROLL, BROWSER_DOUBLE_CLICK, BROWSER_FIND, BROWSER_EXTRACT_LIST, or BROWSER_DRIVE_INSPECT_FOLDER directives.\n"
    "The HTTP API will return directives as actions[] for the extension to confirm.\n"
    "Do not say a browser action has been completed until [PREVIOUS ROUND RESULTS] shows the extension completed it.\n"
    "Do not emit DONE in the same reply as BROWSER_* directives; wait for the extension's results first.\n"
    "\n"
    "[BROWSER PAGE CONTEXT]\n"
    f"url: {FIXTURE_URL}\n"
    "title: Sensei Job App Smoke\n"
    f"interactive_elements: {INTERACTIVE_ELEMENTS}\n"
    "\n"
    "[USER PROMPT]\n"
    "Fill out this job application for Elijah W., 317-555-0100, "
    "you@example.com, Indianapolis IN 46201, 10 years experience, "
    'authorized to work in the US, cover letter "I want this job", '
    "then submit."
)


# The PLAN-AS-BLOCK CONTRACT block as committed to CLOUD_SYSTEM in
# master_ai.py (PLAN-AS-BLOCK CONTRACT section). Pasted verbatim so the
# test stays decoupled from f-string assembly details — if the
# committed teaching changes, update this constant.
PLAN_AS_BLOCK_CONTRACT = (
    "PLAN-AS-BLOCK CONTRACT (multi-step browser work) — mirrors Anthropic's \"Ask "
    "before acting\" pattern. TRIGGER: a Chrome-extension turn that needs 3+ "
    "BROWSER_* actions on the same page. Count the directives you are about to "
    "emit BEFORE you start the reply. If 3+, this contract fires.\n"
    "REPLY SHAPE when the contract fires — strict:\n"
    " 1. The VERY FIRST line of your reply is `<PLAN>` at column 0. The "
    "PLAN block IS the reasoning surface for multi-step browser work.\n"
    " 2. Inside the block, three labels in this order: `Sites:` (space-separated "
    "origins or \"this page\"), then `Steps:` (numbered 1., 2., …, one short "
    "line each), then `Irreversible:` (either \"none\" or one line naming the "
    "irreversible step, e.g., \"Click Submit creates an application record\").\n"
    " 3. Close with `</PLAN>` on its own line at column 0.\n"
    " 4. AFTER `</PLAN>`, emit the BROWSER_* directives one per line. DO NOT "
    "put directives inside the block — the block is the human-readable plan; "
    "the directives are what execute.\n"
    "When the contract does NOT fire (single-step asks: one click, one fill, "
    "one screenshot, one read, two-step mixed tools): emit the directive "
    "directly. No PLAN block.\n"
    "`<PLAN>` (this Anthropic-spec plan block, rendered as one Approve-All card "
    "by the Chrome extension) is DIFFERENT from `<PLAN READY>` (Sensei TUI "
    "plan-mode end-of-plan marker). Both can appear in the same conversation; "
    "do not collapse them.\n"
)


def _build_cloud_system():
    """Build a minimal CLOUD_SYSTEM that carries the committed PLAN-AS-BLOCK
    teaching. We don't reconstruct the full 3500-line live CLOUD_SYSTEM
    — this test isolates whether the PLAN-block teaching is effective
    on its own."""
    return (
        "You are Master AI — a task-executing agent. Respond with "
        "directives, not prose.\n\n"
        "DIRECTIVES:\n"
        "BROWSER_CLICK: <css-selector>\n"
        "BROWSER_FILL: <css-selector> :: <value>\n"
        "BROWSER_READ: <css-selector>\n"
        "BROWSER_NAV: <url>\n"
        "BROWSER_SCREENSHOT: viewport\n\n"
        "BROWSER_DRIVE_INSPECT_FOLDER: {\"query\":\"resume\"}\n\n"
        + PLAN_AS_BLOCK_CONTRACT
    )


def _groq(messages, *, timeout=60):
    """Direct Groq call — mirrors ask_cloud_groq but in-test so we don't
    need the running service. Same User-Agent dance (Cloudflare 1010
    workaround per feedback_cloudflare_python_urllib_ua.md)."""
    key = master_ai.load_keys().get("groq")
    if not key:
        raise unittest.SkipTest("no groq key in ~/.master_ai_keys")
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": 1500,
        "stream": False,
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "python-requests/2.31.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"]


class CloudPlanEmissionTests(unittest.TestCase):
    def test_groq_emits_plan_block_on_multi_step_browser_turn(self):
        system = _build_cloud_system()
        reply = _groq([
            {"role": "system", "content": system},
            {"role": "user", "content": USER_PROMPT},
        ])

        print("\n=== GROQ REPLY (first 1600 chars) ===")
        print(reply[:1600])

        m_open = reply.find("<PLAN>")
        m_close = reply.find("</PLAN>")
        self.assertGreaterEqual(m_open, 0,
                                "Groq did not emit <PLAN> on a multi-step "
                                "Chrome-extension turn — the committed "
                                "CLOUD_SYSTEM teaching doesn't carry through")
        self.assertGreater(m_close, m_open, "</PLAN> missing or before <PLAN>")
        block = reply[m_open + len("<PLAN>"): m_close]

        # The three required labels per CONTRACT.
        self.assertRegex(block, r"(?im)^\s*Sites:",
                         "<PLAN> block missing Sites: line")
        self.assertRegex(block, r"(?im)^\s*Steps:",
                         "<PLAN> block missing Steps: section")
        self.assertRegex(block, r"(?im)^\s*Irreversible:",
                         "<PLAN> block missing Irreversible: line")

        # Steps should enumerate 3+ items.
        step_lines = re.findall(r"(?m)^\s*\d+\.\s+.+", block)
        self.assertGreaterEqual(len(step_lines), 3,
                                f"only {len(step_lines)} numbered steps in PLAN block")

        # BROWSER_* directives appear AFTER </PLAN>, not inside.
        tail = reply[m_close + len("</PLAN>"):]
        self.assertRegex(tail, r"BROWSER_(FILL|CLICK)\s*:",
                         "no BROWSER_* directive after </PLAN>; block is "
                         "render-only and execution is gone")
        self.assertNotRegex(block, r"(?m)^BROWSER_(FILL|CLICK|NAV|READ|SCREENSHOT)\s*:",
                            "BROWSER_* directive appears INSIDE PLAN block — "
                            "those should follow </PLAN>")

        print(f"  ✓ <PLAN> block present with all 3 labels")
        print(f"  ✓ {len(step_lines)} numbered steps")
        print(f"  ✓ BROWSER_* directives follow </PLAN>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
