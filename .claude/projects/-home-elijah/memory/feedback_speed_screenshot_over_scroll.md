---
name: feedback_speed_screenshot_over_scroll
description: "Speed tips for sensei browser — screenshot+resize beats scrolling, hover+click is fastest combo"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: a3a1e028-6d65-4254-b7a4-0644cf8c7e95
---

## Speed rules for sensei browser (operator feedback 2026-05-27)

1. **Screenshot over scroll** — never scroll to read more content; take a screenshot and read it visually, or resize the window tall to see everything at once.
2. **Resize window to read full page** — use `mcp__sensei__resize_window` to make window tall (e.g. 1400-1800px) so full form is visible in one screenshot.
3. **Hover + click is the fastest combo** — hover to position, then click. Works reliably in sensei.
4. **Don't scroll** — scrolling adds round trips. Resize once, screenshot once, act.

**Why:** Operator called out slow pace due to excessive scroll→screenshot cycles. One resize+screenshot replaces 3-4 scroll+screenshot cycles.

**How to apply:** On any new page → `resize_window(width=901, height=1400)` → `screenshot` → read entire page → act. **Zero scrolling. Ever.**

This is the FASTEST full-page read available. One resize + one screenshot beats any number of scroll+screenshot cycles.
