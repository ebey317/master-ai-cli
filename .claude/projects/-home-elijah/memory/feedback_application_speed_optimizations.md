---
name: feedback_application_speed_optimizations
description: Speed optimizations for Fair Chance apply workflow — target 90 seconds or less per application. Resize+screenshot beats scroll. Tighten waits. CSS selectors not text labels. Batch clicks.
metadata: 
  node_type: memory
  type: feedback
  confirmed: 2026-05-27
  originSessionId: 8df69a38-6098-4d34-8d76-be17eb90f0b6
---

# Application Speed Optimizations — Target ≤90 Seconds

Operator does it by hand in 90 seconds. AI must be faster, not slower.

## Rule 1: RESIZE TALL → ONE SCREENSHOT → READ ALL BUTTONS AT ONCE
Never scroll + screenshot + scroll + screenshot. Instead:
```
resize_window(width=900, height=2000)   # make full page visible
screenshot()                             # one capture
read image → find ALL buttons/fields     # done in seconds
```
This eliminates 3-4 scroll cycles per page = saves 10-15 seconds per form step.

## Rule 2: TIGHTEN ALL WAITS
| Action | Old wait | New wait |
|---|---|---|
| Page navigation | 3000ms | 1500ms |
| After autofill click | 3000ms | 1500ms |
| After button click (next page) | 2500ms | 800ms |
| After bridge POST queue | 4000ms | 1500ms |
| Tab open confirmation | 4000ms | 1500ms |

Never use 3000ms+ unless a specific page has proven it needs it.

## Rule 3: CSS SELECTORS NOT TEXT LABELS FOR CLICKS
Text labels waste time hunting. Indeed SmartApply button selectors are FIXED:

| Button | Selector |
|---|---|
| Apply with Indeed | `#indeedApplyButton` |
| Continue (page 1→2) | `button.ia-continueButton` |
| Review details | `button[data-testid="review-details-button"]` |
| Save and continue | `button[data-testid="save-and-continue-button"]` |
| Submit your application | `button[data-testid="submit-button"]` |

Pre-map these. Fire them directly. No read_full hunting mid-flow.

## Rule 4: BATCH SEQUENTIAL CLICKS
Use `mcp__sensei__batch` to fire multiple steps atomically instead of click → wait → screenshot → click:
```json
[
  {"kind": "click", "target": "button.ia-continueButton"},
  {"kind": "wait", "target": "800"},
  {"kind": "click", "target": "button[data-testid='review-details-button']"},
  {"kind": "wait", "target": "800"}
]
```
Only take a screenshot at the START and at any decision point or unexpected state.

## Rule 5: SCREENSHOT ONLY AT DECISION POINTS
Not after every click. Screenshot when:
- Landing on a new page for the first time
- Unexpected result (error, missing field, wrong content)
- Final review page (verify before handing to operator)

NOT: after every button click in a known sequence.

## Rule 6: PRE-CACHE PROFILE BEFORE STARTING
```bash
cp ~/.master_ai_profile.json /tmp/profile_elijah_wilkins.json
```
Do this ONCE at session start. Never re-do mid-flow.

## Optimized Indeed flow (target: under 90 seconds total)

| Step | Action | Target time |
|---|---|---|
| 1 | Navigate to job + confirm Apply button visible | 3s |
| 2 | Bridge POST intercept_popup | 0.5s |
| 3 | Wait for smartapply tab | 1.5s |
| 4 | Click Autofill this page (Simplify) | 0.5s |
| 5 | Wait for autofill complete | 1.5s |
| 6 | Resize tall → screenshot → verify | 2s |
| 7 | Click Continue (CSS) → 800ms → next page | 1.5s |
| 8 | Click Review details → 800ms | 1.5s |
| 9 | Click Save and continue → 800ms | 1.5s |
| 10 | Click Save and continue → 800ms | 1.5s |
| 11 | Resize → screenshot final review page | 2s |
| 12 | Hand to operator for Submit | - |
| **TOTAL** | | **~17 seconds machine time** |

## Key insight from operator
"You were supposed to be faster than me — I can do it in 90 seconds by hand."
AI advantage: no mouse movement time, no reading delay, instant CSS targeting.
The waits are the only real cost — minimize them ruthlessly.

**Related:** [[project_fairchance]], [[workflow_job_application]], [[feedback_indeed_apply_method]]
