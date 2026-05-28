# Job Application Stack — Complete Fix

## Context
The job application workflow has three open holes:
1. **Indeed's Apply button can't be clicked programmatically** — `first_submit_pause` blocks it, Chrome popup blocker blocks the tab, and no interception exists
2. **New tabs opened by any site are invisible to sensei** — no `BROWSER_TAB_SWITCH` action exists, no `window.open` interception
3. **Only Greenhouse + Lever have autofill maps** — Workday, ZipRecruiter embedded forms, SmartRecruiters have none; iCIMS/Taleo/LinkedIn can't be mapped reliably

This plan fixes all three. No workarounds — correct architecture.

---

## Part 1 — The Real Click Fix (content_script.js + service_worker.js + sensei_mcp_server.py)

### 1a. Add `_withPopupIntercept()` helper to content_script.js
Insert near `_shouldResumeSubmit` (~line 3398):

```javascript
function _withPopupIntercept(fn) {
  const _orig = window.open;
  window.open = function(url, _name, _specs) {
    if (url && typeof url === "string") {
      try { chrome.runtime.sendMessage({ type: "SENSEI_BROWSER_TAB_CREATE", url }); } catch (_e) {}
    }
    // Return mock that satisfies widget code checking .focus/.closed/.location
    const mock = { focus() {}, closed: false, location: { href: url || "" }, postMessage() {} };
    return mock;
  };
  try { fn(); } finally {
    setTimeout(() => { window.open = _orig; }, 600);
  }
}
```

### 1b. BROWSER_CLICK handler — add `intercept_popup` flag (~line 3667)
Two changes:
1. Add `&& !action.intercept_popup` to the first_submit_pause deferral condition so `intercept_popup: true` bypasses the pause entirely
2. Wrap `el.click()` at line ~3703: `if (action.intercept_popup) { _withPopupIntercept(() => el.click()); } else { el.click(); }`

### 1c. `_resumePendingSubmit()` — wrap el.click() at ~line 3430
Always intercept in the resume path (resume_click already bypassed first_submit_pause):
```javascript
_withPopupIntercept(() => el.click());
```

### 1d. Add BROWSER_TAB_SWITCH to service_worker.js
After the `SENSEI_BROWSER_TAB_CREATE` block (~line 1030):
```javascript
if (message?.type === "SENSEI_BROWSER_TAB_SWITCH") {
  (async () => {
    try {
      const tab = await chrome.tabs.update(message.tabId, { active: true });
      sendResponse({ ok: true, result: JSON.stringify({ id: tab.id, title: tab.title, url: tab.url }) });
    } catch (err) {
      sendResponse({ ok: false, error: String(err?.message || err) });
    }
  })();
  return true;
}
```

Add matching handler in content_script.js BROWSER_TAB_SWITCH block:
```javascript
if (kind === "BROWSER_TAB_SWITCH") {
  const tabId = Number(action.target);
  if (!Number.isFinite(tabId)) return { ok: false, error: "BROWSER_TAB_SWITCH: target must be numeric tab id" };
  const result = await chrome.runtime.sendMessage({ type: "SENSEI_BROWSER_TAB_SWITCH", tabId });
  return result;
}
```

### 1e. Update sensei_mcp_server.py — two changes
**tool_click** — pass `intercept_popup` through:
```python
def tool_click(args):
    what = str(args.get("what") or "").strip()
    if not what:
        return {"content": [{"type": "text", "text": "click: what is required"}]}
    payload = {"target": what}
    if args.get("intercept_popup"):
        payload["intercept_popup"] = True
    out = _dispatch("BROWSER_CLICK", payload)
    ...
```

**Add tool_tab_switch** after tool_tab_create:
```python
def tool_tab_switch(args):
    """Switch Chrome focus to a tab by its numeric ID (from tab_list)."""
    tab_id = str(args.get("tab_id") or "").strip()
    if not tab_id:
        return {"content": [{"type": "text", "text": "tab_switch: tab_id is required"}]}
    out = _dispatch("BROWSER_TAB_SWITCH", {"target": tab_id})
    rep = f"tab_switch {tab_id} -> {json.dumps(out)[:300]}"
    return {"content": [{"type": "text", "text": rep}]}
```
Register it in the TOOLS dict and schema list.

---

## Part 2 — ATS Maps (ats_maps/ directory)

### 2a. Create `ats_maps/workday.py`
Workday uses `data-automation-id` attributes. Use `*=` (contains) for resilience across tenants.

```python
SELECTORS = {
    'input[data-automation-id*="legalNameSection_firstName"]':  "personal.first_name",
    'input[data-automation-id*="legalNameSection_lastName"]':   "personal.last_name",
    'input[data-automation-id*="email"]':                       "personal.email",
    'input[data-automation-id*="phone"]':                       "personal.phone",
    'input[data-automation-id*="addressSection_city"]':         "personal.city",
    'input[data-automation-id*="addressSection_postalCode"]':   "personal.zip",
    'input[data-automation-id*="linkedIn"]':                    "personal.linkedin",
    'input[data-automation-id*="website"]':                     "personal.website",
    'input[data-automation-id*="howDidYouHear"]':               "custom_answers.why_this_company",
    'div[data-automation-id*="file-upload"] input[type="file"]': "documents.resume_url",
}

FIELD_TYPES = {
    "documents.resume_url": "file",
}

STANDARD_FIELDS = frozenset(SELECTORS.keys())
```

### 2b. Create `ats_maps/smartrecruiters.py`
SmartRecruiters uses `name` attributes on standard HTML inputs.

```python
SELECTORS = {
    'input[name="firstName"]':                 "personal.first_name",
    'input[name="lastName"]':                  "personal.last_name",
    'input[name="email"]':                     "personal.email",
    'input[name="phoneNumber"]':               "personal.phone",
    'input[name="location"]':                  "personal.city",
    'input[name="web.linkedin"]':              "personal.linkedin",
    'input[name="web.portfolio"]':             "personal.website",
    'textarea[name="message"]':                "custom_answers.why_this_company",
    'input[type="file"]':                      "documents.resume_url",
}

FIELD_TYPES = {
    "documents.resume_url": "file",
    "custom_answers.why_this_company": "textarea",
}

STANDARD_FIELDS = frozenset(SELECTORS.keys())
```

### 2c. Update `ats_fingerprint.py` — add SmartRecruiters + ZipRecruiter detection
Add detection blocks for SmartRecruiters (`smartrecruiters.com` in URL, `data-sh-id` in DOM) and ZipRecruiter embedded forms. Wire them to their maps in `tool_autofill_job_form`.

### 2d. Update `tool_autofill_job_form` in sensei_mcp_server.py
Extend the ATS map lookup:
```python
ats_map_module = {
    "greenhouse":      greenhouse,
    "lever":           lever,
    "workday":         workday,
    "smartrecruiters": smartrecruiters,
}.get(ats)
```

---

## Part 3 — Explicit Per-Site Tool Routing Guide

### Create memory file: `reference_job_application_tool_routing.md`
Path: `/home/elijah/.claude/projects/-home-elijah/memory/reference_job_application_tool_routing.md`

Content (full definitions — what tool, what selector, what method, known barriers):

```markdown
# Job Application Tool Routing — Per-Site Definitions

## INDEED (indeed.com)
**Apply button:** `#indeedApplyButton`
**Click method:** `mcp__sensei__click` with `intercept_popup=True`
  - Bypasses `first_submit_pause` AND intercepts `window.open` → routes to `chrome.tabs.create`
  - DO NOT use plain click (deferred), double_click (popup blocked), or js_eval (CSP blocks)
**After click:** `tab_list` → find "Indeed Apply" tab → `tab_switch` to it → wait 4000ms → `autofill_job_form`
**ATS:** IndeedApply widget — no selector map needed; sensei autofill reads form directly
**Barriers:** Simplify extension covers panel → `click("Minimize Minimize")` first. Cloudflare on smartapply subdomain → operator handles manually.

## ZIPRECRUITER (ziprecruiter.com)
**Apply button:** Read `read_full` to find apply button selector (varies by job)
**Click method:** `mcp__sensei__click` with CSS selector (confirmed working — no first_submit_pause on ZipRecruiter)
**After click:** Loads external ATS page (Greenhouse/Lever/Workday) in same tab → `autofill_job_form`
**ATS:** Varies per company — fingerprinted at runtime by `ats_fingerprint.py`
**Barriers:** None confirmed. Use CSS selector from `read_full`, not text label.

## WORKDAY (*.myworkday.com / *.myworkdayjobs.com)
**Apply button:** `button[data-automation-id*="applyButton"]` or text "Apply" — use `read_full` to confirm
**Click method:** `mcp__sensei__click` — standard click, no popup
**After click:** Multi-step wizard loads in same tab
**ATS:** `workday` — `autofill_job_form` uses `ats_maps/workday.py` (`data-automation-id*=` selectors)
**Barriers:** Multi-step form — fill step, click Next, repeat. `waitForPageStable` handles transitions.

## GREENHOUSE (boards.greenhouse.io / jobs.lever.co embedded)
**Apply button:** Usually a standard `<a>` link to a Greenhouse job page
**Click method:** `mcp__sensei__browse` to job URL directly (no popup)
**After click:** Form loads in same tab
**ATS:** `greenhouse` — `autofill_job_form` uses `ats_maps/greenhouse.py`
**Barriers:** Resume upload required — profile must have `documents.resume_url` set to absolute local path.

## LEVER (jobs.lever.co)
**Apply button:** Standard link
**Click method:** `mcp__sensei__browse` to job URL directly
**ATS:** `lever` — `autofill_job_form` uses `ats_maps/lever.py`
**Barriers:** Same as Greenhouse.

## SMARTRECRUITERS (jobs.smartrecruiters.com)
**Apply button:** `button[data-sh-id*="apply"]` or "Apply" text — use `read_full`
**Click method:** `mcp__sensei__click` with CSS selector
**ATS:** `smartrecruiters` — `autofill_job_form` uses `ats_maps/smartrecruiters.py`
**Barriers:** None confirmed.

## LINKEDIN EASY APPLY (linkedin.com)
**Status:** DO NOT AUTOMATE — LinkedIn's ToS prohibits it; form fields are dynamic per-job with no stable selectors; Shadow DOM blocks standard reads.
**Correct approach:** Navigate to job, read the form manually, fill with `fill` tool field-by-field using text from `read_full`. Operator must review each field.

## iCIMS / TALEO / ADP / ORACLE
**Status:** UNKNOWN ATS — no universal selectors exist (each company instance is custom-branded).
**Correct approach:** `autofill_job_form` falls back to LLM-driven fill. Use `read_full` to identify fields, `fill` each individually. Flag these to operator as manual-assist jobs.

## GENERAL RULE: Unknown ATS
1. `read_full` → identify form fields
2. `autofill_job_form` → Phase 1 will say "no selector map"; Phase 2 returns field list
3. Fill each custom field manually with `fill` tool
4. Screenshot before submit for operator review
```

Add to `MEMORY.md` index: `- [Job Application Tool Routing](reference_job_application_tool_routing.md) — Per-site definitions: which click method, which ATS map, known barriers for Indeed/ZipRecruiter/Workday/Greenhouse/Lever/SmartRecruiters/LinkedIn/iCIMS`

---

## Extension Reload
After editing content_script.js and service_worker.js:
```
bash ~/scripts/sensei_extension/redeploy.sh
```

---

## Verification
1. **Indeed click fix:** Navigate to any Indeed job → `click("#indeedApplyButton", intercept_popup=True)` → `tab_list` shows new "Indeed Apply" tab → `tab_switch` to it → screenshot shows form
2. **tab_switch:** `tab_list` → grab any tab id → `tab_switch(tab_id)` → screenshot shows that tab's content
3. **Workday autofill:** Navigate to any `myworkday.com` job page → `autofill_job_form(client_name="Elijah Wilkins")` → fields fill
4. **SmartRecruiters autofill:** Navigate to any `jobs.smartrecruiters.com` page → `autofill_job_form` → fields fill
5. **Routing guide accessible:** Memory file exists and readable at session load
