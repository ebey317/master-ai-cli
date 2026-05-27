---
name: feedback_indeed_apply_method
description: "CONFIRMED WORKING 2026-05-27: intercept_popup bridge POST opens Indeed Apply tab. Tab injection gap: content script does NOT auto-inject into new smartapply tab."
metadata: 
  node_type: memory
  type: feedback
  confirmed: 2026-05-27
  originSessionId: 8df69a38-6098-4d34-8d76-be17eb90f0b6
---

# Indeed Apply Button — CONFIRMED WORKING METHOD (2026-05-27)

## THE FIX THAT WORKS: bridge POST with intercept_popup=true

```bash
curl -s -X POST http://127.0.0.1:8080/extension/queue \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"mcp-default","actions":[{"kind":"BROWSER_CLICK","target":"#indeedApplyButton","intercept_popup":true}]}'
```

**What this does:**
1. `intercept_popup: true` bypasses `first_submit_pause` (no deferral)
2. Before firing `el.click()`, wraps with `_withPopupIntercept()` which overrides `window.open`
3. IndeedApply widget runs normally, generates session token, calls `window.open(url)`
4. `_withPopupIntercept` catches the URL and routes it to `chrome.runtime.sendMessage({ type: "SENSEI_BROWSER_TAB_CREATE", url })`
5. Service worker opens a REAL tab via `chrome.tabs.create()` — not popup-blocked
6. **Result:** New tab opens at `smartapply.indeed.com` with full apply form loaded

**Confirmed result:** `smartapply.indeed.com/beta/indeedapply/form/resume-selection-module/resume-selection` opened with Elijah_Wilkins_Resume loaded.

## ⚠️ OPEN GAP: Content script does NOT inject into new smartapply tab

The sensei extension badge ("MCB") does NOT appear on the new `smartapply.indeed.com` tab. This means:
- `autofill_job_form` cannot run on that tab programmatically
- `tab_switch` works (chrome.tabs.update) but sensei cannot CONTROL the smartapply tab
- Operator must currently handle the smartapply form manually (Simplify's "Autofill this page" can fill it, or operator fills manually)

**Root cause:** Content script injection requires either (a) extension permission for that origin in manifest, or (b) tab was open before extension loaded. New tabs opened via `chrome.tabs.create` from the service worker may not get content script injected if the manifest doesn't list `smartapply.indeed.com` in `content_scripts.matches`.

**Fix needed:** Add `"https://smartapply.indeed.com/*"` to `content_scripts.matches` in `manifest.json`. Then reload extension. After that, sensei badge will appear on the apply tab and autofill can run.

## Step-by-step workflow (current working state)

1. Navigate to job: `indeed.com/jobs?vjk=JOB_ID`
2. Click the job card to load the detail panel with "Apply with Indeed" button
3. Bridge POST with `intercept_popup: true` (see curl above)
4. Wait 4000ms
5. `tab_list` → confirm new smartapply tab appeared
6. **Operator takes over the smartapply tab** (Simplify autofill or manual)
7. Operator submits when ready

## Why MCP server tool_click doesn't work yet

`mcp__sensei__click` with `intercept_popup=True` requires `sensei_mcp_server.py` to be restarted (`master-ai-ui.service`). Until restarted, use the bridge curl POST directly.

## Previous methods (superseded)

- `double_click` on `#indeedApplyButton` — requires real user gesture first (Chrome popup blocker)
- `resume_click` command — bypasses first_submit_pause but NOT popup blocker
- `js_eval` — blocked by indeed.com CSP (`unsafe-eval` not in script-src)

**Related:** [[workflow_job_application]], [[feedback_css_selector_click_method]], [[reference_job_application_tool_routing]]
