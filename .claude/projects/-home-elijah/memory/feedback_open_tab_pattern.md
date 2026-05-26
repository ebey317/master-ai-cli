---
name: feedback_open_tab_pattern
description: "Standard pattern for \"open tab\" / \"open mcp tab\" commands — one tab to Google via sensei tab_create"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 59ae4039-a0c3-42f9-b77f-970dfbab8089
---

When operator says "open tab" or "open mcp tab":
1. Call `mcp__sensei__tab_create` with `url="https://google.com"` — ONE tab only.
2. Wait ~2s, then screenshot to confirm it loaded.
3. No narration before. Show screenshot after.

**Why:** Operator established this pattern 2026-05-26 — single tab to Google, always through MCP/sensei path.
**How to apply:** Never open two tabs unless explicitly asked for multiple. Never use bare `google-chrome <url>`.

Dev tools pipeline confirmed same session:
- `console_logs` → BROWSER_CONSOLE relay in content_script.js → `__SENSEI_CONSOLE_EVENTS__`
- `network_requests` → SENSEI_BROWSER_NETWORK in service_worker.js

[[feedback_no_claude_chrome_extension]] [[feedback_stop_clicking_ask_first]]
