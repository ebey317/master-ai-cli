---
name: session-account-context
description: "Current account context — which side (Pro vs API/CLAF), active model, billing status. Auto-updated on every save. Determines workflow, settings, routing."
metadata: 
  node_type: memory
  type: project
  updated_ts: 2026-05-28T10:55:00Z
  originSessionId: 19104ada-9c1c-4ce9-a137-9d9d7d5a41e7
---

# Session Account Context

**Last save:** 2026-05-28 10:55 AM  
**Current account side:** API/CLAF (Console key active, ANTHROPIC_BASE_URL=http://localhost:8000)  
**Active model:** Haiku 4.5  
**Default settings:** ~/projects/claf/.claude/settings.json  

## Pro Side
- Path: ~/.claude/settings.json
- Auth: OAuth (claude.ai Max)
- Status: available, no per-token tracking
- Model: (check settings.json)

## API/CLAF Side (ACTIVE)
- Path: ~/projects/claf/.claude/settings.json
- Auth: ANTHROPIC_CONSOLE_KEY in keychain
- Status: pay-per-token via Tier 6 (Anthropic provider)
- Model: Haiku 4.5
- Launch: ~/projects/claf/launch.sh
- Status check: ~/scripts/claf_status.sh

## Workflow State
- Last commit: "Flag business portfolio for auto-load on session start; sync all 12 projects from office spreadsheet" (3907f4b)
- Branch: refine-biovega-phase0-visibility
- Pending: None (working tree clean)
- Office: MasterAI_Office.ods synced to portfolio state

**On next session start:** Inject this context so workflow continues seamlessly without "which side?" friction.
