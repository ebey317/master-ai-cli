---
name: project_fairchance
description: "Fair Chance — autofill job application service. $2/app, 20/hr = $40/hr. Indeed + ZipRecruiter confirmed working end-to-end 2026-05-27."
metadata: 
  node_type: memory
  type: project
  originSessionId: 00050b0e-3121-4d68-8c5e-337d7fd7aa6b
  confirmed: 2026-05-27
---

# Fair Chance

Autofill-assisted job application service built on the same infrastructure as Elijah's own job applications.

**Core concept:** Collect client info → build `/tmp/profile_{slug}.json` → run `autofill_job_form` with `client_name` → form fills automatically.

## Business model (confirmed 2026-05-27)
- **$2 per application submitted**
- **Target speed:** 3 minutes per application
- **Throughput:** 20 applications/hour
- **Revenue:** $40/hour — operator-owned, tax-free
- **5 apps in 15 minutes** is the benchmark

## Platforms confirmed working
| Platform | Status |
|---|---|
| **Indeed** | ✅ End-to-end confirmed — intercept_popup → Simplify Autofill → walk steps → operator submits |
| **ZipRecruiter** | ✅ Confirmed — read_full CSS click → autofill_job_form |
| **Honest Jobs** | ✅ Same flow as ZipRecruiter |

## Indeed workflow (confirmed end-to-end 2026-05-27)
1. Navigate to Indeed job with "Apply with Indeed" button
2. Bridge POST with `intercept_popup:true` on `#indeedApplyButton`
3. New tab opens → Simplify panel → click **Autofill this page**
4. Walk pages: Resume → Education → Experience → Review
5. **Operator hits "Submit your application"** (final step — never automated)

**Project root:** `~/projects/fairchance/`  
**Client profile template:** `~/projects/fairchance/profiles/client_profile_template.json`  
**Runtime client profiles:** `/tmp/profile_{firstname-lastname}.json` (session-scoped)

**Hard stops (never auto-fill):** SSN, DOB, banking, legal e-sign, felony free-text — client must be present.

## Next build priorities
1. Fix manifest.json — add `smartapply.indeed.com` to `content_scripts.matches` (removes Simplify dependency, full programmatic control)
2. Client intake form → profile JSON generator
3. Multi-job batch apply per client session
4. Application tracker (job title, company, date, status)

Connects to [[project_pipeline_six_ideas]], [[workflow_job_application]], [[feedback_indeed_apply_method]], [[project_sensei_tab_injection_gap]].
