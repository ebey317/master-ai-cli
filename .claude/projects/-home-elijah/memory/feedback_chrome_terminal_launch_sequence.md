---
name: feedback_chrome_terminal_launch_sequence
description: Chrome terminal bootstrap sequence — detach MCP tab before closing bare tab or you kill the bridge.
metadata: 
  node_type: memory
  type: feedback
  locked: 2026-05-27
  originSessionId: 83563952-ad45-4005-a182-eb3a34951740
---

When Chrome isn't running and must be launched from terminal, follow this exact sequence:

1. `google-chrome` from terminal → Chrome opens with one bare tab (throwaway)
2. `tab_create` via sensei → MCP-controlled tab created in the same window
3. **Detach the MCP tab** → drag it out to its own independent window
4. Close the bare terminal-launched tab → that window closes safely, MCP tab survives

**Why:** If the MCP tab stays in the same window as the bare tab and you close that window (or the bare tab is the last one left), Chrome closes the window and takes the bridge down with it. Detaching first makes the MCP tab independent — closing the bare tab can't touch it.

**How to apply:** Never skip step 3. Detach before any cleanup. Failure to detach = deleting yourself.
