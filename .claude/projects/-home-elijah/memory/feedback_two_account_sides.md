---
name: feedback_two_account_sides
description: Two completely separate Claude accounts — Pro side vs API/CLAF side. Never confuse them. Ask which side before doing account-level work.
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 31e45bbd-9d4b-421b-b756-65a44969c724
---

There are TWO separate Claude accounts. They are never mixed.

## Pro Side
- **What**: claude.ai Max subscription, $100/mo flat fee
- **Auth**: OAuth credentials (`~/.claude/.credentials.json`)
- **Session**: default Claude Code launch (no launch.sh)
- **Billing**: flat monthly, no per-token charges
- **Settings**: `~/.claude/settings.json` (global)
- **Status line**: NONE — operator removed it 2026-05-24

## API / CLAF Side
- **What**: Anthropic Console pay-per-token API account
- **Auth**: `ANTHROPIC_CONSOLE_KEY` in keychain (NOT `ANTHROPIC_API_KEY`)
- **Session**: launched via `~/projects/claf/launch.sh` → sets `ANTHROPIC_BASE_URL=http://localhost:8000`
- **Billing**: per-token on whatever hits Tier 6 (anthropic provider in CLAF)
- **Settings**: `~/projects/claf/.claude/settings.json` (project-level)
- **Status line**: `claf_status.sh` — shows offgrid% | local/cloud/anthropic call counts

## Rules
- NEVER touch one side's settings/config while working in the other
- If unsure which side a task belongs to → **ASK** before doing anything
- Never mix the Console key into the Pro OAuth session or vice versa
- The two streams are intentionally separated; do not bridge them

**Why:** Operator built this separation deliberately. Pro is the subscription runtime. API/CLAF is the pay-per-use escalation path with local routing. Burning Pro credits on work meant for CLAF (or vice versa) is a real cost error. [[feedback_account_separation_strict]] [[project_claf_throttle_and_account_separation]]
