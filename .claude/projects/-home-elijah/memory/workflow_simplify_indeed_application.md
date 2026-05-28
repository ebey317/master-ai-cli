---
name: workflow_simplify_indeed_application
description: Step-by-step loop for filling Indeed job applications using the Simplify browser extension
metadata: 
  node_type: memory
  type: project
  originSessionId: b664b9e8-c3bd-4f93-8896-4d5fa9215463
---

# Indeed + Simplify Application Loop

Confirmed pattern 2026-05-27 — HVAC Service Technician @ Appel Heating & Air Conditioning.

## The Loop — EXACT ORDER (repeat on every new page)

1. **Screenshot** — `screenshot` → `Read` image immediately. Under 2 seconds.
2. **Minimize Simplify** — Click "Minimize" (NOT the X). Always before autofill.
3. **Autofill** — Click "Autofill this page". Wait 2000ms for "Autofill complete!"
4. **Read full page** — `scroll top` → screenshot → `scroll down` → screenshot until bottom is reached.
5. **Review unfilled fields** — Check for blanks. Most fields auto-filled by Simplify. Known manual field:
   - **Interview availability (Day/Time/Timezone):**
     - Day → `Weekday` ✅ (usually pre-filled)
     - Time → `Anytime (8am - 9pm)` ✅ (usually pre-filled)
     - Timezone → Search "Indiana" returns no match → select **America/Chicago** (closest to Indianapolis). Do not spend more than 5 seconds on this field.
6. **Advance** — `hover` → `click` the page CTA button:
   - Page 1: **Continue**
   - Screener questions: **Continue**
   - Review education: **Save and continue**
   - Review work experience: **Save and continue**
   - Review application: **STOP — wait for operator approval before Submit**
7. **Wait 2000ms** → `scroll top` → screenshot → repeat loop.

## Hard Rules

- **STOP at "Submit your application"** — Never submit without explicit operator approval in chat.
- Never click X on Simplify — always Minimize.
- Enter key does NOT reliably trigger Continue buttons — use hover → click.
- Timezone combobox is iframe-scoped — js_eval won't work. Use `fill` to open dropdown, accept closest match.
- Bio facts to have ready: Indianapolis IN, Eastern Time (Chicago as fallback), 8 years HVAC experience, EPA Type II = Yes, Work authorized = Yes, Speaks English = Yes, Education = High school or equivalent.

## Page Sequence (Indeed SmartApply)
1. Resume upload (page 1/2)
2. Screener questions — employer-specific (page 2/2)
3. Highlight resume details
4. Review education
5. Review work experience
6. **Review + Submit** ← STOP HERE

See also: [[workflow_job_application]], [[feedback_application_speed_optimizations]], [[feedback_indeed_apply_method]]
