---
name: project_sensei_tab_injection_gap
description: "OPEN BUG: sensei content script does not inject into new tabs (smartapply.indeed.com, external ATS pages). MCB badge missing = can't autofill or control. Fix: add origins to manifest.json content_scripts.matches."
metadata: 
  node_type: memory
  type: project
  status: open
  confirmed: 2026-05-27
  originSessionId: 8df69a38-6098-4d34-8d76-be17eb90f0b6
---

# Sensei Tab Injection Gap — Content Script Doesn't Cover New Tabs

## The problem

When sensei opens a new tab via `chrome.tabs.create()` (the intercept_popup path), the content script does NOT inject into that tab. The sensei extension badge ("MCB") is absent on the new tab. Without the content script present:
- Cannot `autofill_job_form` on the new tab
- Cannot `click`, `fill`, `read`, `screenshot` on the new tab
- `tab_switch` (chrome.tabs.update) works but that's all

**Operator observed this directly:** "I don't see the MCB on the tab so that means you can't do multi tab. You have it sometimes or sometimes you don't. It needs to be up there." — 2026-05-27

## Affected URLs

- `https://smartapply.indeed.com/*` — Indeed apply form
- Any external ATS page opened from ZipRecruiter/LinkedIn/etc. that's not in the manifest

## Root cause

`manifest.json` `content_scripts.matches` only lists origins known at install time. New URLs not in that list don't get the content script injected automatically when a tab opens.

## Fix

Add the missing origins to `content_scripts.matches` in `~/scripts/sensei_extension/manifest.json`:

```json
"content_scripts": [{
  "matches": [
    "https://*.indeed.com/*",
    "https://smartapply.indeed.com/*",
    "https://*.greenhouse.io/*",
    "https://*.lever.co/*",
    "https://*.myworkday.com/*",
    "https://*.myworkdayjobs.com/*",
    "https://*.smartrecruiters.com/*",
    "https://*.ziprecruiter.com/*",
    "https://*.linkedin.com/*"
  ],
  ...
}]
```

After editing: reload extension at chrome://extensions → click reload on Sensei → confirm MCB badge appears on all listed domains.

## Current workaround

Operator manually fills the smartapply form using Simplify's "Autofill this page" button (which is already present on the tab). This works but removes automation.

## Priority: HIGH

Every application workflow depends on sensei controlling the apply tab. Until this is fixed, multi-tab apply automation is broken for any site not already in the manifest.
