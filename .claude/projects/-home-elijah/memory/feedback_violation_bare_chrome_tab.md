---
name: feedback_violation_bare_chrome_tab
description: ⚠️ VIOLATION CAPTURED 2026-05-28: Bare Chrome tab — Bash google-chrome used
metadata:
  type: feedback
---

NEVER call `google-chrome <url>` from Bash. That creates a tab outside the MCP group that sensei cannot drive.

**Why:** Violation caught by learning hook on 2026-05-28. Rule §1a: all tabs via mcp__sensei__tab_create only.

**How to apply:** When a URL needs to open: mcp__sensei__tab_create → mcp__sensei__browse within that tab. Never shell out to google-chrome with a URL argument.

Last captured: 2026-05-28 19:29:40
