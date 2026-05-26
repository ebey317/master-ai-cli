---
name: project_session_start_memory_hook
description: Build a hook that reads MEMORY.md + pinned memories on first prompt of each thread only — not every prompt.
metadata: 
  node_type: memory
  type: project
  locked: 2026-05-25
  originSessionId: 72464a6a-c6d5-4ec8-b849-9fddf94b0a3f
---

Operator requirement locked 2026-05-25:

**"Set a hook, an agent, or whatever you need to do to check your memory on initial login so you have these things in every thread — not every prompt, but thread. So as soon as I login and give the first thread, you're reading memory and you're getting notes about everything and we continue on from there."**

**What this means:**
- On the FIRST prompt of each new thread/session: automatically read MEMORY.md + pinned project memories
- On subsequent prompts within the same thread: do NOT re-read (too expensive, too noisy)
- The goal: no more "where were we" — I should already know

**Implementation path:**
- Enhance `~/.claude/hooks/userpromptsubmit_inject.sh` (Layer 0 Channel B)
- Add a session-tracking state file: `~/.claude/.session_id` or use PID
- On first prompt: inject full MEMORY.md + contents of top 5 pinned memories into the prepend
- On subsequent prompts in same session: inject only the heartbeat / schema status (current behavior)
- "First prompt" detection: compare current `$CLAUDE_SESSION_ID` env var (or timestamp) against last recorded value in `~/.claude/.last_session_id`

**Files to modify:**
- `~/.claude/hooks/userpromptsubmit_inject.sh` — add first-prompt detection + memory injection
- New state file: `~/.claude/.last_session_id` — tracks session boundary

**Status:** NOT YET BUILT. This is a standing requirement.

Related: [[feedback_verified_on_screen]], [[project_chrome_extension_clone]]
