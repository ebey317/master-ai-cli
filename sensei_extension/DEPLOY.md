# Sensei Extension — Deployment & Verification

How to take this session's committed changes from "merged" to "running"
and verify the full Anthropic-spec UX is live.

## TL;DR

One-shot script for the backend half (rebuilds local model + restarts
the service + verifies /health, then prints the manual Chrome steps):

```bash
bash ~/scripts/sensei_extension/redeploy.sh
```

Or run the underlying steps yourself:

```bash
# Activate the backend changes (CLOUD_SYSTEM teaching + auto-routing
# + UnboundLocalError fix). Shared-infra restart — admin terminal:
ollama create master-ai -f ~/scripts/Modelfile-master-ai
systemctl --user restart master-ai-ui.service

# Activate the side-panel changes (Approve-All plan card, mode-name
# copy, friendly 503 UX, brand icons). Chrome → chrome://extensions
# → reload the Sensei extension (the circular-arrow icon on its
# tile). Then refresh the target tab.

# Verify with the deterministic smoke suite (each test stands alone):
python3 ~/scripts/test_api_handle_wedge.py            # 2 tests, ~0.5s
python3 ~/scripts/test_extension_e2e_smoke.py         # 1 test, ~5-15s
python3 ~/scripts/test_chrome_headless_e2e.py         # 1 test, ~1-2s
python3 ~/scripts/test_cloud_plan_emission.py         # 1 test, ~1s
python3 ~/scripts/test_plan_block_emission.py         # 2 tests, ~10s
python3 ~/scripts/test_drive_inspect_handler.py       # 2 tests, ~3s
```

If every test prints `OK`, the spec UX is live on the cloud lane and
the local lane fallback is intact.

## What activates on each step

### `systemctl --user restart master-ai-ui.service`

Reimports `master_ai.py` and `stt_server.py` so the running process
picks up this session's backend commits:

- **`522b3b8`** — `/chat` dispatch lock has a 120s timeout; runaway
  local inference can't poison other requests. Returns HTTP 503
  `{error: "system_busy", retry_after_s: 15}` past timeout.
- **`0c1f202`** — `/chat/continue` no longer 500s with
  `UnboundLocalError` when the body omits `mode`.
- **`4246ef8`** — CLOUD_SYSTEM carries the PLAN-AS-BLOCK CONTRACT;
  cloud-lane models emit `<PLAN>…</PLAN>` on multi-step browser
  turns per the Anthropic spec.
- **`36faf18`** — orchestrate() auto-routes Chrome-extension turns
  with `[BROWSER PAGE CONTEXT]` to `cloud_fast` (Groq llama-3.3-70b)
  whenever a Groq key is present. No `fast:` prefix needed — the
  system self-determines.

### Reload extension in `chrome://extensions`

Reloads `side_panel.js / .css / .html` and `manifest.json` so Chrome
picks up the UI commits:

- **`8da1f8d`** — Toolbar icon stops being the puzzle-piece. 16/32/
  48/128 PNG icon set wired into `manifest.icons` and
  `action.default_icon`.
- **`01c2c4d`** — Mode dropdown reads "Ask before acting" / "Plan
  only" / "Act without asking" per Anthropic spec.
- **`04d717e`** — 503 wedge-busy now surfaces as a friendly retry
  message ("Sensei is busy with another task on the local model
  — please retry in about 15 seconds.").
- **`9a548df`** — Multi-step rounds (3+ pending BROWSER_* actions)
  render as one Approve-All plan card with the action list and
  brand-accent border, instead of N individual cards.

### Manual fixture validation

```text
1. Open chrome://extensions, reload Sensei.
2. Open file:///home/elijah/scripts/sensei_extension/test/job_app_smoke.html
3. Open the side panel; mode = "Ask before acting".
4. Send: fill out this job application for Elijah W., 317-555-0100,
   ebey317@gmail.com, Indianapolis IN 46201, 10 years experience,
   authorized to work in the US, cover letter "I want this job",
   then submit.
5. Expected: ONE Approve-All card listing 11 actions, brand-accent
   teal border, "Approve all" button.
6. Click Approve all → all 11 actions dispatch sequentially →
   page shows "Application SUBMITTED at <iso>".
```

If you also have a Groq key in `~/.master_ai_keys`, the `/chat`
response carries a literal `<PLAN>…</PLAN>` block in the chat
transcript above the dispatched cards. If not, the card UI ships
the same spec UX regardless.

## What's NOT activated by this deployment

- **Phase 2.1b file upload** — résumé `<input type="file">` upload
  bridge in `content_script.js`. Tracked at
  `~/MD/handoff_phase_2_1b_file_upload.md` for Codex's content_script
  refactor commit. The job-app fixture intentionally treats résumé
  as optional so the rest of the chain still completes.
- **Pupil/extension brand-color alignment** — Pupil uses brand blue
  `#2266cc`; the extension now uses teal `#0f766e` (icon set + side
  panel + options page all match each other). Cross-surface
  harmony is a product call (align Pupil to teal, or rebrand the
  extension back to blue).

## Rollback

Every commit this session is independently revertable:

```bash
cd ~/scripts && git revert <commit-sha>
```

The risky surfaces are limited: `522b3b8` (lock timeout — if 120s
proves too aggressive, raise it) and `36faf18` (auto-routing — if
you want local-only on extension by default, revert and users
prefix `fast:` explicitly). Neither alters audit/safety
infrastructure.

## Test reference

Each test is hermetic and runs against the live service after
restart. Run any in isolation; failures point at one specific
seam:

| Test | What it proves | Needs |
|------|----------------|-------|
| `test_api_handle_wedge.py` | Lock timeout + ApiHandleBusy raise on contended `_API_HANDLE_LOCK` | Nothing external |
| `test_extension_e2e_smoke.py` | Backend produces 11 correctly-ordered actions; selectors resolve on fixture HTML; required fields covered | Running stt_server, Ollama up |
| `test_chrome_headless_e2e.py` | Real headless Chrome executes 11 actions on the fixture; submit handler fires; `__senseiJobAppSmoke.submitted === true` | `google-chrome` installed |
| `test_cloud_plan_emission.py` | Groq llama-3.3-70b emits the spec `<PLAN>…</PLAN>` block on the committed CLOUD_SYSTEM teaching | Groq key in `~/.master_ai_keys` |
| `test_plan_block_emission.py` | Same as above but routed through the live `/chat` endpoint after restart | Running stt_server (post-restart), Groq key |

The deterministic-verify standard lives in
`feedback_codex_multi_step_standard.md`.
