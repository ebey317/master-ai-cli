# Sensei Extension Job-App End-to-End Smoke Checklist

Goal: prove the Chrome extension can fill a multi-field application and
click Submit without per-field approval — the headline demo for the
"extension self-automates a job application until submitted" goal.

This test uses only a local static page:

`~/scripts/sensei_extension/test/job_app_smoke.html`

Companion to `LOOP_CHECKLIST.md` (single-field smoke). Same shape, more
fields, plus an explicit pass/fail line for the file-upload sub-feature
which is currently expected to FAIL until Phase 2.1b lands.

## Setup

1. Confirm backend is active:

   ```bash
   systemctl --user is-active master-ai-ui.service
   ```

   Expected: `active`

2. Reload the unpacked extension in `chrome://extensions`.

3. Open the test page in Chrome:

   ```text
   file:///home/elijah/scripts/sensei_extension/test/job_app_smoke.html
   ```

4. DevTools on the test page. Required panels: `Console`, `Network`.

5. In another terminal:

   ```bash
   journalctl --user -u master-ai-ui.service -f
   ```

   ```bash
   tail -f ~/.master_ai_audit_typed.jsonl
   ```

## Test Prompt

1. Open the Sensei side panel.
2. Set mode to `Auto`.
3. Send exactly:

   ```text
   fill out this job application for Elijah W., 317-555-0100,
   ebey317@gmail.com, Indianapolis IN 46201, 10 years experience,
   authorized to work in the US, cover letter "I want this job",
   then submit.
   ```

## Expected `/chat` Request

`POST /chat` body should include:

```json
{
  "mode": "auto",
  "source": "chrome_extension",
  "page_context": {
    "url": "file:///home/elijah/scripts/sensei_extension/test/job_app_smoke.html",
    "title": "Sensei Job App Smoke",
    "interactive_elements": "..."
  }
}
```

`page_context.interactive_elements` must mention the form fields by
their accessible names:

```text
textbox "First name" selector=#firstName
textbox "Last name" selector=#lastName
textbox "Email" selector=#email
textbox "Phone" selector=#phone
textbox "City" selector=#city
combobox "State" selector=#state
textbox "ZIP" selector=#zip
spinbutton "Years of experience" selector=#yearsExperience
radio "Yes" / "No" (workAuth group)
textbox "Cover letter" selector=#coverLetter
input file "Résumé" selector=#resume
button "Submit application" selector=#submitButton
```

If `interactive_elements` is missing or truncates the radio group, the
content_script's page-context extractor cannot see the form correctly.

## Expected `/chat` Response — Round 1

The response should contain BROWSER_FILL actions for all required text
fields (and BROWSER_CLICK for the workAuth=yes radio), then either a
BROWSER_CLICK on `#submitButton` OR a continuation. Acceptable shapes:

```json
{
  "actions": [
    { "kind": "BROWSER_FILL",   "target": "#firstName => Elijah" },
    { "kind": "BROWSER_FILL",   "target": "#lastName => W." },
    { "kind": "BROWSER_FILL",   "target": "#email => ebey317@gmail.com" },
    { "kind": "BROWSER_FILL",   "target": "#phone => 317-555-0100" },
    { "kind": "BROWSER_FILL",   "target": "#city => Indianapolis" },
    { "kind": "BROWSER_FILL",   "target": "#state => IN" },
    { "kind": "BROWSER_FILL",   "target": "#zip => 46201" },
    { "kind": "BROWSER_FILL",   "target": "#yearsExperience => 10" },
    { "kind": "BROWSER_CLICK",  "target": "input[name=\"workAuth\"][value=\"yes\"]" },
    { "kind": "BROWSER_FILL",   "target": "#coverLetter => I want this job" },
    { "kind": "BROWSER_CLICK",  "target": "#submitButton" }
  ],
  "done": false
}
```

The model may split this across rounds via the M9 continuation loop —
that's fine as long as `/chat/continue` carries it through to Submit.

Failure signs:

- Empty `actions[]` on round 1
- `done: true` before submit fires
- `BROWSER_FILL` with no selector parse (`target: "First name"` w/o `:: value`)
- Action sequencing skips workAuth radio click

## Expected Auto-Run Behavior

- First action card renders with `Allow once / Always allow site / Decline`.
- Click `Always allow site` (origin = `file://`).
- All remaining actions auto-run in sequence — no further user input.
- Per `side_panel.js:shouldAutoRunAction`, BROWSER_FILL / BROWSER_CLICK
  are safe on approved origin unless classified `sensitive_fill` /
  `purchase` / `auth`. None of the job-app fields trip those gates
  (verified in `classifyBrowserAction` regex list).

## Expected Page Result

After all actions dispatch, the page should show:

```text
Application SUBMITTED at <iso timestamp>

{
  "firstName": "Elijah",
  "lastName": "W.",
  ...
  "workAuth": "yes",
  "resumeName": "",
  "resumeSize": 0
}
```

The `resumeName` / `resumeSize` being empty is **expected** in the
current build — file upload is deferred to Phase 2.1b. The submit
handler does NOT require the résumé field; it's optional in this
fixture by design.

In DevTools Console:

```text
[Sensei job app smoke] submit success {"submitted":true,...}
```

## Expected Audit Lines

In `~/.master_ai_audit_typed.jsonl`, one row per executed action:

```text
extension_action_result BROWSER_FILL success    (firstName)
extension_action_result BROWSER_FILL success    (lastName)
extension_action_result BROWSER_FILL success    (email)
extension_action_result BROWSER_FILL success    (phone)
extension_action_result BROWSER_FILL success    (city)
extension_action_result BROWSER_FILL success    (state)
extension_action_result BROWSER_FILL success    (zip)
extension_action_result BROWSER_FILL success    (yearsExperience)
extension_action_result BROWSER_CLICK success   (workAuth=yes)
extension_action_result BROWSER_FILL success    (coverLetter)
extension_action_result BROWSER_CLICK success   (submitButton)
turn_terminal
```

`final_state.permission` should be `always_allow_site` on the first
action and `auto` on the rest.

## Expected `/chat/continue`

After all actions report, `POST /chat/continue` fires with the full
`action_results` array. The model should NOT propose more fills
(everything passed) and should reply with a `done: true` summary like:

```text
Submitted the application — firstName=Elijah, ..., workAuth=yes.
```

## Pass Criteria

Pass only if ALL true:

1. `mode` in `/chat` is `auto`.
2. Round 1 response (or accumulated across continuation rounds) emits
   exactly 11 actions covering every required field + the radio + Submit.
3. After one `Always allow site` click, the remaining 10 actions
   auto-run without any further user input.
4. `window.__senseiJobAppSmoke.submitted === true` after the chain ends.
5. `window.__senseiJobAppSmoke.missing` is `[]`.
6. Console logs `[Sensei job app smoke] submit success`.
7. Audit JSONL has 11 `extension_action_result` rows ending in `success`
   plus one `turn_terminal`.
8. `/chat/continue` rounds carry `parent_turn_id` through to a clean
   `done: true` terminal.
9. No 503 from `/chat` (wedge protection didn't false-fire).

If any line fails, the end-to-end goal is not verified.

## Known Gaps (NOT failure for this checklist)

- **Résumé file upload** — the `#resume` input stays empty; this is
  Phase 2.1b work (content_script.js refactor pile, owned by Codex).
  The fixture's submit handler does NOT require this field, so the
  rest of the chain still completes. Once Phase 2.1b lands, re-run
  with the prompt amended to include `upload résumé from <path>` and
  add a 12th expected action `BROWSER_FILL: #resume :: file://...`.
- **Multi-page navigation** — single-page form by design. A `/page2`
  variant is the natural follow-on once this passes.
- **`<PLAN>...</PLAN>` UX** — the user sees N action cards stream in,
  not one Approve-All card. Phase 5 work; functionally equivalent on
  approved origin (auto-run path) so deferred behind the e2e demo.

## If It Stalls

Same decision table as `LOOP_CHECKLIST.md`, plus these job-app-specific
failure modes:

| Symptom | Read This | Likely Break |
| --- | --- | --- |
| Fills stop after ~3-4 fields | `/chat/continue` request body | Round budget too low; check `_API_DEFAULT_ROUND_BUDGET` |
| `workAuth` radio not clicked | `/chat` response actions | Model doesn't see radio groups in `interactive_elements` |
| `state` select fills as text not value | content_script Console | `setElementValue` not setting `<select>` selectedIndex |
| Submit fires before all fields filled | action ordering in `/chat` response | Model emitted Submit too eagerly |
| Submit blocked because workAuth empty | page `#result` text | Radio click action didn't execute or hit wrong element |
| `/chat` returns 503 mid-flow | side panel Console | Wedge protection fired — local Ollama stuck on another request |
