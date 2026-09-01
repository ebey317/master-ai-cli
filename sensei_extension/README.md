# Sensei — Master AI's Browser Limb

![icon](icons/icon-128.png)

A Chrome extension that gives Master AI's local agent (Sensei) a hand
on the active browser tab. Drives forms, clicks, navigation, and page
reads on your behalf — talking to a **local-first** backend on your
own machine, not a hosted service.

Mirrors Anthropic's Claude-for-Chrome permission and plan-and-approve
pattern ([reference](https://code.claude.com/docs/en/chrome)) while
keeping the model and your data on-box by default.

## What it does

- **Sidebar chat.** Open the side panel on any page; describe what
  you want done. Sensei reads the page, picks selectors, and proposes
  one or more `BROWSER_*` directives (CLICK, FILL, READ, NAV,
  SCREENSHOT, WAIT, SCROLL, FIND, EXTRACT_LIST, DRIVE_INSPECT_FOLDER).
- **Three permission modes** (dropdown on the chat composer):
  - **Ask before acting** — Sensei proposes; you approve each card.
    Default safe state.
  - **Plan only** — Sensei drafts an execution plan with no
    dispatch. Pure thinking mode; press "go" later to actually run.
  - **Act without asking** — Sensei dispatches routine actions
    automatically on approved sites. The always-confirm list still
    pauses for purchases, deletions, auth, and sensitive form fields.
- **Site permissions.** First time Sensei touches a site, three
  choices: *Allow once* / *Always allow site* / *Decline*. Always-allow
  domains are remembered in `chrome.storage.local`; manage them in
  the options page.
- **Always-confirm list.** Even in *Act without asking*, these never
  flow through automatically:
  - Purchases / payments / checkout flows
  - Permanent deletions and account removals
  - Sign-in / auth / OAuth grants
  - Password / SSN / credit-card / API-key fields
  - Navigation to checkout-style URLs
- **Loop awareness.** After dispatching a round of actions, the side
  panel reports results back to the backend's M9 continuation loop;
  Sensei decides whether to propose more actions, ask a clarifying
  question, or emit `DONE:`. Round budget caps runaway loops.

## What you get with a multi-step ask (Anthropic-spec plan flow)

The backend's CLOUD_SYSTEM and the master-ai Modelfile both carry
the **PLAN-AS-BLOCK CONTRACT**: any Chrome-extension turn that needs
3+ `BROWSER_*` actions opens its reply with a `<PLAN>…</PLAN>` block
listing:

- the **sites** Sensei will touch,
- the **steps** numbered 1-N, and
- any **irreversible** action in the chain.

The side panel renders that as one *Approve-All* card on first
touch; subsequent actions on the same approved site flow without
re-prompting (subject to the always-confirm list above).

## Local setup

1. Run the Master AI backend (`stt_server.py` on port 8080):
   ```bash
   systemctl --user enable --now master-ai-ui.service
   ```
2. Generate the shared token (first install only):
   ```bash
   head -c 32 /dev/urandom | xxd -p > ~/.master_ai_extension_token
   ```
3. Open `chrome://extensions`, enable Developer mode, and **Load
   unpacked** → point at this directory.
4. Click the Sensei toolbar icon to pin it.
5. Open the extension options (right-click the icon → Options) and
   paste the token from step 2.

Every backend request carries `X-Master-AI-Token`; `/chat` sends
`source`, `session_id`, `page_context` (URL, title, accessibility-tree
interactive_elements), and `mode`. The backend stays on `127.0.0.1`
by default.

## File layout

- `manifest.json` — MV3 manifest. Permissions, side panel, options.
- `service_worker.js` — side-panel open behavior + active-tab
  screenshot capture (`chrome.tabs.captureVisibleTab`).
- `side_panel.html / .css / .js` — chat, mode dropdown, action
  cards, allow/always/decline buttons, screenshot rendering, local
  Whisper `/stt` mic.
- `content_script.js` — page-context collection (visible text +
  interactive-elements list with accessible names) + `BROWSER_*`
  action dispatch on the page (`setElementValue`, `el.click()`,
  `chrome.tabs.update` for NAV), including Google Drive row extraction
  and empty-folder detection for Drive inspection.
- `options.html / .css / .js` — backend URL, token, default mode,
  session ID, résumé file path (Phase 2.1), approved sites manager,
  and remote MCP server allowlist.
- `sensei_native_host.py` (repo root) + `install_native_host.sh` —
  optional native messaging bridge for local desktop-bridge requests.
- `icons/` — 16 / 32 / 48 / 128 PNG icon set.
- `test/` — deterministic smoke fixtures:
  - `loop_smoke.html` + `LOOP_CHECKLIST.md` — single-field FILL +
    CLICK + REPORT + CONTINUE loop.
  - `job_app_smoke.html` + `JOB_APP_CHECKLIST.md` — 10-field
    job-application end-to-end fill + submit.

## Safety patterns

- **Pre-flight irreversible-action gate.** `classifyBrowserAction()`
  in `side_panel.js` runs five regex categories against every
  proposed action target. Sensitive matches require explicit per-
  action approval regardless of mode.
- **Backend dispatcher owns sensitivity.** Even when the extension
  is in *Act without asking*, the stt_server `actions[]` shape
  carries `status` (`planned` / `waiting_for_approval` / `running`)
  and `gated_by` (e.g., `irreversible_heuristic:sensitive_fill`).
  Trust the backend, not the model.
- **Backend wedge protection.** If a runaway local inference holds
  the `/chat` dispatch lock past 120 seconds, the backend returns
  HTTP 503 `system_busy` with a `retry_after_s` hint instead of
  hanging. The side panel renders this as a clean retry message
  (with a `fast:` cloud-lane hint).
- **Result envelope truth.** Every dispatched directive's outcome
  flows back through `/extension/action_result` and is exposed to
  the model as `[PREVIOUS ROUND RESULTS]` in the next round, with
  `observed_tab_url` as ground truth — the model can't claim
  success a navigation that didn't happen.
- **REMOTE_MCP approval gate.** Remote MCP calls are not browser
  actions and never auto-run. The side panel requires explicit
  approval and only sends `tools/list` or `tools/call` to configured
  server URLs.

## Known limits

- **Résumé file upload (Phase 2.1b).** Text fields fill cleanly; the
  `<input type="file">` upload bridge (DataTransfer + File construction
  in `content_script.js`) is in progress. Track at
  `~/MD/handoff_phase_2_1b_file_upload.md`.
- **Local model `<PLAN>` emission.** The default local model
  (`qwen2.5:7b` via the master-ai Modelfile) doesn't reliably
  emit the `<PLAN>…</PLAN>` block on every multi-step turn —
  it's a small-model instruction-following limit, not a teaching
  gap. The functionally equivalent behavior (one "Always allow
  site" approval → subsequent actions auto-flow) still holds. For
  the explicit PLAN-block UX today, prefix the prompt with `fast:`
  (Groq) or `deep:` (DeepSeek-R1 via OpenRouter) — both cloud lanes
  carry the same CLOUD_SYSTEM teaching and follow it reliably.
- **Chrome must be running for schedules.** Workflow schedules use
  `chrome.alarms`; if Chrome is closed and background apps are off,
  the browser cannot fire the alarm. Late wakeups are marked
  `delayed_execution` in the saved schedule.

## Native Messaging + Workflows

Install the native messaging host after loading the unpacked extension
and copying its Chrome extension ID:

```bash
bash ~/scripts/sensei_extension/install_native_host.sh <extension-id>
```

Use **Record** in the side panel to capture clicks and fills into a
saved shortcut. Saved shortcuts appear in the Shortcuts dock and can be
run immediately or scheduled daily, weekly, monthly, or annually.

## Verifying it works

Two deterministic tests (require `python3` + a running Chrome):

```bash
# Live backend round-trip + selector validation against the fixture
python3 ~/scripts/test_extension_e2e_smoke.py

# Real headless Chrome execution of 11 actions against the fixture,
# asserts window.__senseiJobAppSmoke.submitted === true
python3 ~/scripts/test_chrome_headless_e2e.py
```

Manual: open `file:///home/elijah/scripts/sensei_extension/test/
job_app_smoke.html`, side-panel Auto, run the prompt from
`JOB_APP_CHECKLIST.md`, verify all 10 fields fill and Submit clicks.
