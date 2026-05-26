---
name: feedback_verified_on_screen
description: "Everything must be verified AND shown on screen — no silent actions, no assumed success."
metadata: 
  node_type: memory
  type: feedback
  locked: 2026-05-25
  originSessionId: 72464a6a-c6d5-4ec8-b849-9fddf94b0a3f
---

Operator rule locked 2026-05-25:

**"I need everything to be verified and everything to be on screen make it a memory"**

**Why:** I was taking browser actions, making API calls, and reporting outcomes without showing the operator what actually happened on screen. That's not acceptable — he needs to SEE it to trust it.

**How to apply:**
- After every consequential action (form fill, submit, API call, file write), show the operator a visible result — screenshot, read output, or tool return value.
- Never say "done" or "submitted" without showing proof (screenshot, confirmation text, or read output).
- Verified = independently checked, not just "the tool didn't error."
- On screen = operator can see it, not just me narrating what I think happened.
- If I can't get visible confirmation, say so explicitly: "I can't verify — please check your screen."

Related: [[feedback_stop_clicking_ask_first]], [[feedback_operator_must_see_authenticated_actions]], [[feedback_mcp_browser_must_be_visible]]
