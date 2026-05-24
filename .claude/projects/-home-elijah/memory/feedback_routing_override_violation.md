---
name: routing_override_violation
description: I must not accept /model commands that circumvent CLAF orchestration
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 260e1b1c-a725-45bf-8e3c-4342a1dfa728
---

**Rule:** Never accept a `/model` override that bypasses the CLAF orchestrator's routing logic.

**Why:** You have hybrid-mode routing in place (`CLAF_MODE=hybrid`, `CLAF_LOCAL_MODEL=fast-agent:latest`). This is a deliberate architectural decision: local-first, cloud escalation only on hard tasks. When I accept `/model Haiku`, I pin the entire session to an expensive Anthropic model and silently violate the routing you've wired.

**How to apply:** 
- When `/model` is called, check if it would override the CLAF routing.
- If yes: explain the conflict to the operator and ask for explicit authorization with reasoning (e.g., "override orchestration for this session because...").
- If no explicit auth, continue under CLAF control.
- The routing is the product; model-override commands are budget-breaking exceptions, not defaults.
- CLAF audit logging is already wired (`/tmp/orchestra_audit.log`, `orchestra_display.py`). Use it to show what's actually routing and why.

**Current state:** `CLAF_MODE=hybrid`, local model = `fast-agent:latest`. Respect this unless explicitly overridden with reasoning.
