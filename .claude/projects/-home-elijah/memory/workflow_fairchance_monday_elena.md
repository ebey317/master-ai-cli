---
name: workflow-fairchance-monday-elena
description: ⚠️ Fair Chance job-application handoff contract — what Claude posts to Monday board 18415270563 so Elena (monday agent) picks it up and runs the rest.
metadata: 
  node_type: memory
  type: project
  locked: 2026-05-28
  originSessionId: 19104ada-9c1c-4ce9-a137-9d9d7d5a41e7
---

# Fair Chance → Monday → Elena handoff contract

**Locked 2026-05-28.** When Claude Code submits a job application (Fair Chance, $2/app), it reports the result to Monday so Elena (active monday platform agent, trigger = any update on this board) takes over.

**Board:** `18415270563` ("01 — Fair Chance | LIVE"), workspace 15737966 (account `ebey317s-team`).

## What to post per application
1. **Create item** — name convention: `Candidate – Job Title – Employer`
2. **Set columns:**
   | Field | Column ID | Notes |
   |---|---|---|
   | Job Title | `text_mm3s15q4` | exact title from posting |
   | Employer / Company | `text_mm3sk61y` | |
   | Application Date | `date_mm3s5dfz` | YYYY-MM-DD |
   | Job Posting URL | `link_mm3sxh8d` | direct listing URL |
   | Pipeline Stage | `color_mm3sfrk4` | set to **"Applied"** (label id 0) |
   | Notes / result | `long_text_mm3sgrdx` | submitted / failed + detail |
3. **Post an item update** summarizing: job title, employer, application date, job URL, result (submitted/failed). The UPDATE is what fires Elena's trigger.

## After that
Elena handles the rest (her trigger fires on the update). Operator: "I handle the rest from there."

## Hard-won facts about this channel
- The board is the cross-agent mailbox. No public URL exists; Monday API has NO agent mutations — can't @mention/trigger an agent programmatically. The only trigger is **posting an update to the board** (operator activated Elena's trigger in the UI 2026-05-28).
- Both Claude's connector and Elena post under the SAME identity (Elijah Wilkins, id 104422259). Route by TEXT, never by author/creator.
- Test item used during setup: `12129790790`.

Related: [[project_monday_portfolio]], [[project_fairchance]]
