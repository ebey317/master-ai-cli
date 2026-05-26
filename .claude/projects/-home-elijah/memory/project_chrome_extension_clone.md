---
name: project_chrome_extension_clone
description: "Clone a proven Chrome MV3 extension, add operator's tool suite, add debug/devtools mode — the 100% accurate extension project."
metadata: 
  node_type: memory
  type: project
  locked: 2026-05-25
  originSessionId: 72464a6a-c6d5-4ec8-b849-9fddf94b0a3f
---

Operator requirement locked 2026-05-25:

**"The one thing that we need is a 100% accurate chrome extension so why not clone one here that's proven to work and put my tools on it and make sure it can open the bugging mode because I got bugs that needs to be worked out."**

**What this means:**
- Take a proven, known-working Chrome MV3 extension as the baseline
- Add operator's tools: autofill, browser bridge commands (BROWSER_* directives), form-filling pipeline
- Add proper debug/devtools mode so bugs can be investigated directly in Chrome DevTools
- Target: 100% accuracy — no silent failures, no unverified submits

**Base candidate:**
- `~/scripts/sensei_extension/` — current sensei MV3 extension, operator's hands
- Audit it first: what's proven, what's broken, what's missing
- Identify the "proven working" baseline vs the parts that need replacement

**Key requirements:**
1. Debug mode — extension must have a DevTools panel or at minimum a visible debug log accessible from chrome://extensions developer mode
2. Form fill accuracy — every fill operation must be verifiable before submission
3. BROWSER_* directive parity — all current commands must work identically or better
4. Operator visibility — no headless changes; everything on screen

**Project location:** `~/projects/master_ai_extension/` (to be created)

**Status:** NOT YET STARTED. This is the current primary technical project.

Related: [[feedback_data_specialist_identity]], [[feedback_verified_on_screen]], [[feedback_stop_clicking_ask_first]], [[project_session_start_memory_hook]]
