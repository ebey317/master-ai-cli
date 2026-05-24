---
name: feedback-no-chrome-extensions-page
description: "Hard no: never navigate to chrome://extensions or any chrome:// URL. Operator manages extension reloads manually."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
---

## The rule

Never navigate to `chrome://extensions` or any `chrome://` URL — not via sensei, not via claude-in-chrome, not via any shell command.

When an extension reload is needed, tell the operator what to do in plain language and wait. Do not attempt the navigation yourself.

**Why:** chrome:// pages are outside the operator-visible MCP tab group and cannot be driven reliably by any of the browser tools. The operator manages extension state manually.

**How to apply:** When a code change to a Chrome extension requires a reload, say: "Reload Sensei at chrome://extensions — find the extension and click the ↻ button." Stop there. Do not issue any navigation tool call.
