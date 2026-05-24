---
name: feedback_no_claude_chrome_extension
description: "⚠️ UPDATED 2026-05-24. claude-in-chrome is UNAVAILABLE — returns 'Browser extension is not connected' every session. Do NOT call those tools. sensei is the ONLY browser surface. When sensei observability fails, hand to operator — no escalation path via claude-in-chrome."
metadata:
  node_type: memory
  type: feedback
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
  updatedSessionId: current
---

## ⚠️ RULE UPDATED 2026-05-24 — claude-in-chrome is UNAVAILABLE

**Confirmed as of 2026-05-24:** Every session, `tabs_context_mcp(createIfEmpty: true)` returns:
> "Browser extension is not connected. Please ensure the Claude browser extension is installed and running."

**Do NOT call any `mcp__claude-in-chrome__*` tools.** They will all fail with this error.

The extension may be installed in `~/.claude.json` (cachedChromeExtensionInstalled: true, pairedDeviceId: 78566a11) but it does **not** connect in practice. Treat as unavailable permanently until operator explicitly re-enables and confirms it works.

## Current browser stack

| Tool | Status | Notes |
|---|---|---|
| **sensei** | ✅ Primary + only browser path | All browse/click/fill/screenshot/js_eval/read |
| **secretary** | ✅ Available | Autonomous multi-step task runner |
| **claude-in-chrome** | ❌ UNAVAILABLE | Do not call. Always fails. |

## Failure handling when sensei observability fails

Per [[retry-failure-schema]]: if screenshot + js_eval + read ALL fail on sensei → STOP immediately. **Hand to operator.** There is no automated escalation path — claude-in-chrome is gone.

Sensei OBSERVABILITY_FAILURE signs:
- `screenshot` returns `"no injectable tab for MCP action"`
- `js_eval` returns `"failure"` consistently
- `read` returns `"no injectable tab for MCP action"`

When all three fail: stop, tell operator what URL is open and what action is needed, let them do it manually.

## Historical note

- 2026-05-23: rule was updated saying claude-in-chrome IS installed and paired
- 2026-05-24: operator confirms it's unavailable — revert to "do not use" posture

## Cross-references

- [[retry-failure-schema]] — OBSERVABILITY_FAILURE = zero retries, hand to operator
- [[mcp-browser-must-be-visible]] — if all 3 channels fail, STOP
- [[operator-must-see-authenticated-actions]] — sensei actions still must be visible
