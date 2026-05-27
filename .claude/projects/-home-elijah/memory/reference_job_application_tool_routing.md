---
name: reference_job_application_tool_routing
description: "Per-site job application routing — which click method, ATS map, and known barriers for Indeed/ZipRecruiter/Workday/Greenhouse/Lever/SmartRecruiters/LinkedIn/iCIMS"
metadata: 
  node_type: memory
  type: reference
  updated: 2026-05-27
  originSessionId: 8df69a38-6098-4d34-8d76-be17eb90f0b6
---

# Job Application Tool Routing — Per-Site Definitions

## INDEED (indeed.com)
**Apply button:** `#indeedApplyButton`  
**Click method:** `mcp__sensei__click` with `intercept_popup=True`
- Bypasses `first_submit_pause` AND intercepts `window.open` → routes to `chrome.tabs.create`
- DO NOT use plain click (deferred by first_submit_pause), double_click (popup blocked by Chrome), or js_eval (CSP blocks eval on indeed.com)

**After click:**
1. `mcp__sensei__tab_list` → find new "Indeed Apply" / smartapply.indeed.com tab
2. `mcp__sensei__tab_switch` with that tab's numeric ID → brings it to front
3. Wait 4000ms → `mcp__sensei__autofill_job_form` with `client_name="Elijah Wilkins"`

**ATS:** IndeedApply widget — autofill reads the rendered smartapply form directly  
**Barriers:**
- Simplify extension covers job-detail panel → `mcp__sensei__click("Minimize Minimize")` FIRST
- Cloudflare challenge on smartapply.indeed.com → operator handles manually (usually auto-resolves)

---

## ZIPRECRUITER (ziprecruiter.com)
**Apply button:** CSS selector from `mcp__sensei__read_full` (varies per job — not a fixed selector)  
**Click method:** `mcp__sensei__click` with exact CSS selector string (confirmed working — no first_submit_pause on ZipRecruiter)  
**After click:** External ATS page loads in same tab → `mcp__sensei__autofill_job_form`  
**ATS:** Varies per company — fingerprinted at runtime by `ats_fingerprint.py` (greenhouse/lever/workday/smartrecruiters/unknown)  
**Barriers:** None confirmed. Use CSS selector from `read_full`, not text label.

---

## WORKDAY (*.myworkday.com / *.myworkdayjobs.com)
**Apply button:** `button[data-automation-id*="applyButton"]` or text "Apply" — confirm with `read_full`  
**Click method:** `mcp__sensei__click` — standard click, no popup  
**After click:** Multi-step wizard loads in same tab  
**ATS:** `workday` — `autofill_job_form` uses `ats_maps/workday.py` (`data-automation-id*=` selectors)  
**Barriers:** Multi-step form — fill step, click Next, repeat. `waitForPageStable` handles transitions.

---

## GREENHOUSE (boards.greenhouse.io)
**Apply button:** Usually a standard `<a>` link → navigate directly with `mcp__sensei__browse`  
**Click method:** `mcp__sensei__browse` to job URL  
**After click:** Form loads in same tab  
**ATS:** `greenhouse` — `autofill_job_form` uses `ats_maps/greenhouse.py`  
**Barriers:** Resume upload required — profile must have `documents.resume_url` as an absolute local path.

---

## LEVER (jobs.lever.co)
**Apply button:** Standard link  
**Click method:** `mcp__sensei__browse` to job URL  
**ATS:** `lever` — `autofill_job_form` uses `ats_maps/lever.py`  
**Barriers:** Same as Greenhouse.

---

## SMARTRECRUITERS (jobs.smartrecruiters.com)
**Apply button:** `button[data-sh-id*="apply"]` or "Apply" text — confirm with `read_full`  
**Click method:** `mcp__sensei__click` with CSS selector  
**ATS:** `smartrecruiters` — `autofill_job_form` uses `ats_maps/smartrecruiters.py` (`name=` attribute selectors)  
**Barriers:** None confirmed.

---

## LINKEDIN EASY APPLY (linkedin.com)
**Status:** DO NOT AUTOMATE — LinkedIn ToS prohibits automation; form fields are dynamic per-job with no stable selectors; Shadow DOM blocks standard reads.  
**Correct approach:** Navigate to job manually. Use `mcp__sensei__fill` field-by-field with values from profile. Operator must review each field.

---

## iCIMS / TALEO / ADP / ORACLE
**Status:** UNKNOWN ATS — no universal selectors (each company instance is custom-branded).  
**Correct approach:**
1. `mcp__sensei__read_full` → identify form fields
2. `mcp__sensei__autofill_job_form` → Phase 1 reports "no selector map"; Phase 2 returns field list
3. Fill each custom field manually with `mcp__sensei__fill`
4. Screenshot before submit for operator review

---

## GENERAL RULE: Unknown ATS
If `autofill_job_form` returns "ATS detected as 'unknown'":
1. `read_full` → map all visible fields
2. Fill standard fields (name, email, phone, address) with `fill` tool
3. Flag to operator: "ATS unknown — manual review required before submit"

---

## INTERCEPT_POPUP WORKFLOW (Indeed + any site using window.open for apply)
```
1. mcp__sensei__browse → indeed.com/jobs?vjk=JOBID
2. If Simplify open: mcp__sensei__click → "Minimize Minimize"
3. mcp__sensei__click → "#indeedApplyButton" with intercept_popup=True
4. mcp__sensei__tab_list → find new tab (smartapply.indeed.com)
5. mcp__sensei__tab_switch → that tab's numeric ID
6. mcp__sensei__wait → 4000ms
7. mcp__sensei__autofill_job_form → client_name="Elijah Wilkins"
```

**Related:** [[workflow_job_application]], [[feedback_indeed_apply_method]], [[feedback_css_selector_click_method]]
