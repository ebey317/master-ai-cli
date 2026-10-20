# Pupil GUI Eval — 2026-10-20

First execution-verified pass over the Pupil GUI (`pupil.html` + `stt_server`
API on `localhost:8080`). Checks drive the real UI against the real backend;
nothing is judged from the model's or the page's claims.

## Environment

- Viewports: desktop 1280×900, phone 390×844
- Browser: Playwright-driven Chrome (persistent profile; the service worker
  history below is part of the findings)
- Backend: live `stt_server` (Ollama `master-ai` + cloud lanes)

## Results

| # | Check | Result | Evidence |
|---|-------|--------|----------|
| 1 | Boot / first paint | **PASS** | title "Pupil IDE - Master AI"; DOMContentLoaded 427 ms, load 429 ms (local) |
| 2 | Console clean | **PASS** | 0 errors / 0 warnings on a current load (old builds 404'd `/favicon.ico`) |
| 3 | Health/mode/model header | **PASS** | Health `active`, Mode `auto`; "Local model" label truthfully names the Ollama probe |
| 4 | Model state truth | **PASS** | composer shows `Auto (router)` + `Pinned — poolside::poolside/laguna-s-2.1`, matching `/models` (`auto:false`) |
| 5 | Saved chats hygiene | **PASS** | 4 real sessions + "44 test session(s) hidden"; artifacts not deleted |
| 6 | Chat round-trip | **PASS** | user bubble → "waiting for Master AI…" → assistant `PONG` → footer `Route: cloud \| Model: fireworks \| 2132 ms` |
| 7 | Skills panel | **PASS** | 4 cards (`ai-job-search`, `cleanup-first-run`, …) |
| 8 | Models panel | **PASS** | 10 provider cards incl. Auto-routing CLAF |
| 9 | MCP panel | **PASS** | 3 cards incl. a real `email` STDIO server (`~/scripts/email_mcp/server.py`) |
| 10 | Status panel | **PASS** | 5 cards; memory facts 35, last route cloud, RAM available |
| 11 | Mode switch | **PASS (caveat)** | clicks POST `/mode` (200) and log "Mode changed to plan/auto"; rapid scripted toggles can display out of order (late responses overwrite state — no in-flight sequencing) |
| 12 | Accessibility baseline | **PASS (minor)** | 53 controls, 0 unnamed (labels/aria/placeholder); landmarks nav + header; 2 aria-live regions; `lang=en`. Finding: no `<main>` landmark |
| 13 | Mobile 390×844 | **FAIL → FIXED here** | before: page 1433 px tall, composer at y=1233, **Send at y=1389 (below fold)** — typing required scrolling past the whole transcript. After: doc = viewport (844), composer pinned 644–844, Send visible 800–830, transcript scrolls internally |
| 14 | Desktop 1280×900 regression | **PASS** | doc = viewport (900); composer pinned 711–900; Send visible |
| 15 | Stale service-worker shells | **FIXED earlier (#15)** | returning profiles pin the old HTML until one cache-busted load; SW v2 serves HTML network-only (`no-store`), so it self-heals |

## Findings & follow-ups

1. **Mobile layout (fixed in this PR)** — the ≤860 px media query switched the
   app to flow layout (`height:auto; overflow:auto`, static panels), so content
   height pushed the composer below the fold. Now the app keeps viewport height
   (`100dvh`), the sidebar collapses to a 59 px top bar, and only `.messages`
   scrolls.
2. **Mode-switch ordering (open)** — no guard against out-of-order `/mode`
   responses; deliberate single clicks are fine, rapid programmatic toggles can
   leave the header showing an older mode until the next status poll.
3. **Missing `<main>` landmark (open)** — add `<main>`/`role="main"` around the
   panel container for screen-reader structure.
4. **Deployment note** — the served file is the runtime copy
   (`~/projects/master-ai-cli/pupil.html` via `~/scripts`); repo merges don't
   update it. Keep the runtime sync step explicit.
5. **Persistent-profile SW** — clients that saw v1 may need one cache-busted
   load (`?v=N`) before v2 takes over; subsequent loads are network-only.

## Method

Interactive Playwright session (browser harness), viewport resize, real
network + console capture, DOM-state assertions. Screenshot:
`eval/pupil-gui-mobile-fixed.png` (390×844, composer pinned).
