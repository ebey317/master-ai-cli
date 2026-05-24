---
name: project_fairchance
description: "Fair Chance — autofill job application service for friends, family, and people who need a real shot"
metadata: 
  node_type: memory
  type: project
  originSessionId: 00050b0e-3121-4d68-8c5e-337d7fd7aa6b
---

# Fair Chance

Autofill-assisted job application service built on the same infrastructure as Elijah's own job applications.

**Core concept:** Collect client info → build `/tmp/profile_{slug}.json` → run `autofill_job_form` with `client_name` → form fills automatically.

**Project root:** `~/projects/fairchance/`  
**Client profile template:** `~/projects/fairchance/profiles/client_profile_template.json`  
**Runtime client profiles:** `/tmp/profile_{firstname-lastname}.json` (session-scoped)

**Hard stops (never auto-fill):** SSN, DOB, banking, legal e-sign, felony free-text — client must be present.

**Pipeline items:**
- Intake form (collect info → generate profile JSON)
- Session-based client storage
- Multi-job batch apply per client
- Application tracker
- Referral growth

Connects to [[project_pipeline_six_ideas]] (was listed as "Fair Chance Employer").
