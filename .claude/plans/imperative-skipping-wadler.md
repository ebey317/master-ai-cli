# Find the Missing Layer — Automation Accuracy Investigation

## Context

**Operator's complaint 2026-05-23:**
> "find working automation and accuracy we need to find a way for you to be more accurate... You was able to fill out an application then, but now you can't. Something ain't right. There's a missing layer here and I'm gonna find that layer and I'm not gonna do anything else until I find that layer."

He suspects there's a tool / capability / configuration / hook / memory rule / classifier behavior that explains why past automation succeeded and recent attempts have failed. He's open to the possibility that the agent (me) is deliberately holding back.

This plan documents what an Explore agent found, ranks the candidates, and proposes a single first verification step — not a multi-week build.

## The most likely missing layer (PRIMARY)

**Stale memory rule blocks an installed, paired tool surface.**

| What the memory says | What the system actually is |
|---|---|
| `feedback_no_claude_chrome_extension.md`: "We do NOT use the claude-in-chrome extension... calling those tools always returns 'Browser extension is not connected'" | `~/.claude.json` has `cachedChromeExtensionInstalled: true`, `claudeInChromeDefaultEnabled: true`, `pairedDeviceId: "78566a11-ccd4-455e-abae-35176c46750e"` (named "Browser 1"), and `hasCompletedClaudeInChromeOnboarding: true` |
| Routes ALL browser automation through sensei | The richer `mcp__claude-in-chrome__*` surface (computer with native left_click/type/screenshot/scroll/key/zoom, form_input, javascript_tool, read_page accessibility tree, browser_batch) is fully available — and Claude has been avoiding it |

The memory rule was written assuming the extension was unused. It's now stale. Sensei has known fragility (screenshot bridge bug, js_eval failures on certain pages, read truncation at element #2) — those are exactly the failures that produced the MEGA OTT click-loop. The claude-in-chrome `computer` tool wouldn't have those specific failure modes because it's a different code path (native Chrome extension, not Python proxy).

**Likely past success:** the "filled out an application" reference is probably from a session BEFORE the no-claude-chrome-extension memory was locked, when claude-in-chrome was the path. Or it was sensei before the screenshot bridge broke.

## Operator's diagnostic framework (4 root causes — supplied by operator)

The operator named four typical root causes for MCP-browser-tool accuracy failures. Mapping each to the evidence here:

### 1. Reliance on Accessibility Trees (vs full DOM)
> AI tools rely on a stripped-down accessibility tree to save tokens. If a button is hidden / improperly tagged / no aria-label, clicks fail.

**Maps to:** sensei `read` truncates `interactive_elements` at element #2. That IS an accessibility-tree-style stripped view — and it stops before the relevant content on modal-heavy pages. claude-in-chrome's `read_page` returns a real accessibility tree with `ref_id` element references that can target unlabeled elements by structural position. **Fix #1 applies directly.**

### 2. Dynamic UI State Desync
> AI snapshots page at time t; form auto-update / dropdown expand / modal pop = internal map desynced.

**Maps to:** MEGA OTT click-loop. The "How to earn gift credit" modal opened (or didn't) — Claude couldn't tell because every observability channel was broken simultaneously, and the snapshot Claude had was from BEFORE the click. claude-in-chrome's `computer` tool with screenshot-after-action would close this loop; the visibility hook now also enforces "screenshot all three channels broken = STOP" so the loop is bounded. **Fix #2 (refresh-before-execute) and fix #3 (review logs) both apply.**

### 3. Cookie / Login / Session Traps
> Spawned browser instance lacks personal cookies / passwords / session history → CAPTCHAs, sign-in walls, un-tailored layouts.

**Maps to:** **THE LIKELY ROOT CAUSE.** sensei almost certainly spawns its own headless browser process — separate session from the operator's Chrome. That means: no login state on most sites, default un-personalized layouts, CAPTCHA bait. claude-in-chrome is an **extension** attached to the operator's actual Chrome — cookies, sessions, logins, autofill are all live. **This is the killer differentiator.** The operator's past success on "filling out an application" almost certainly happened in the extension-attached context, not a sensei-spawned context.

### 4. Missing Native Context (off-target clicks)
> No MCP-native integration in page → agent guesses coordinates → wrong clicks.

**Maps to:** Coordinate-based clicks via sensei have been historically brittle (operator's `feedback_sensei_tennis_lessons` memory has rules like "copy/paste over fill" and "locate-first" to mitigate). claude-in-chrome's `computer` tool supports both coordinate AND ref-based clicks (`scroll_to` with `ref_id`), letting the model target by accessibility-tree reference instead of pixel coordinates. **Reduces guess-clicking.**

## Operator's prescribed fixes

> 1. **Switch to an In-Browser Context** — extension in operator's active Chrome, not isolated headless window
> 2. **Review the Console/Terminal Logs** of the MCP server during the task
> 3. **Prompt Specificity** — explicit visual descriptions, refresh containers first

Fix #1 IS the verification step in this plan. Fix #2 maps to inspecting `~/.claude/retry_policy.log` and any sensei stdout/stderr (where does it log?). Fix #3 is a Claude behavior change for after the tool migration — write more specific element descriptions, refresh DOM between steps.

## Other plausible layers (ranked, secondary to the 4 above)

1. **Two-stage auto-mode classifier over-blocks self-modification** — `~/.claude.json` has `"twoStageClassifier": true`. Today it blocked me from testing hook scripts I'd written. False-positive territory. Not browser-related but contributes to friction.

2. **Visibility + retry guards installed TODAY add interception** — both `operator_visibility_guard.sh` (PreToolUse) and `retry_policy_guard.sh` (PostToolUseFailure) are intentional and correct. Side effect: every authenticated MCP call has a hook decision point. Right policy, new behavior.

3. **My own capability profile self-limiting** — `feedback_capability_profile_play_to_strengths.md` (locked today) tells me to hold back on extension/terminal work unless explicitly asked. May cause under-attempting. The operator wrote this rule based on the failures it's now causing me to avoid — circular.

## What past success looked like (forensic)

The agent found:
- `project_pipeline_six_ideas.md` references "Fair Chance Employer (Dezzy Zero) — automated job application machine, 200 apps/30 days, already in motion"
- Both Indeed and ZipRecruiter MCP tools are pre-allowed in `~/.claude/settings.local.json`
- The sensei `fill` tool exists and operator has documented click+fill patterns (`feedback_sensei_tennis_lessons.md`)

The successful past session most likely used **sensei before the screenshot bridge broke** OR **claude-in-chrome before the no-extension memory was locked**. Either way: a path Claude is currently not using.

## Critical files

| File | What it tells us |
|---|---|
| `~/.claude.json` | Current extension state (installed + paired) — CONTRADICTS the stale memory |
| `~/.claude/projects/-home-elijah/memory/feedback_no_claude_chrome_extension.md` | The stale rule to be updated or removed |
| `~/.claude/settings.local.json` | Pre-allowed MCP tools list (sensei yes, claude-in-chrome no) |
| `~/.claude/hooks/operator_visibility_guard.sh` | New PreToolUse layer (today) |
| `~/.claude/hooks/retry_policy_guard.sh` | New PostToolUseFailure layer (today) |
| `~/.claude/retry_policy.log`, `~/.claude/operator_visibility.log` | Evidence of what's been firing |
| `~/scripts/` (sensei entry points, if any) | Sensei MCP health — screenshot bridge specifically |

## Operator's expert diagnostic (THE actual missing layer)

After the local audit's claude-in-chrome finding, operator supplied a deeper diagnosis specific to job-application automation:

> Tools like the Anthropic Chrome Extension and Simplify Copilot do not use pure AI vision or raw DOM scraping. **The secret layer is hardcoded ATS parsing maps paired with an internal relational database.**

The 3 layers tools like Simplify build BENEATH the LLM:

### Layer 1 — Standardized Data Schema (Source of Truth)
Parse resume ONCE into structured JSON. Database/file with mapped keys: `personal.first_name`, `experience[].role`, `disclosures.visa_sponsorship`, etc. LLM never re-parses raw resume text per application.

### Layer 2 — ATS-Specific DOM Fingerprinting
On page load, identify the ATS:
- Greenhouse → `#main-application-form` or `name="job_application[...]"`
- Lever → `.application-form` or actions pointing to `jobs.lever.co`
- Workday → `data-automation-id` attributes

Once fingerprinted, abandon generic LLM clicking and run a **deterministic mapping dictionary** specific to that ATS.

### Layer 3 — Code-First, LLM-Last Architecture (the flip)
- **Phase 1 (Code):** ATS fingerprint → run programmatic script → fill 100% of standard fields (name, email, resume, LinkedIn) WITHOUT consulting the LLM
- **Phase 2 (LLM):** Scan remaining empty fields → extract ONLY the unique custom questions (essays, "why this company")
- **Phase 3 (Fill):** Pass just those 2-3 questions + profile context to LLM → LLM generates targeted text → paste into remaining inputs

**The structural mistake the operator identified:** letting the LLM drive 100% of the process. Code should drive 90%, LLM handles 10%.

## Current stack inventory

Confirmed via direct file reads:

| Layer | What it is |
|---|---|
| **MCP server** | `~/scripts/sensei_mcp_server.py` — Python, JSON-RPC over stdio, 6 tools (chat, browse, click, fill, read, search) |
| **Bridge** | `~/scripts/sensei_bridge.py` — HTTP at `127.0.0.1:8080` |
| **Extension** | `~/scripts/sensei_extension/` — **Manifest V3 + vanilla JavaScript** (no Plasmo/React). Name: "Sensei — browser limb" v0.1.1. Permissions: `scripting`, `debugger`, `sidePanel`, `tabs`, `nativeMessaging`. Files: `content_script.js` (3,091 lines), `service_worker.js` (931), `side_panel.js` (3,804). |
| **Brain** | Local Ollama (`master_ai.py` agent) with cloud escalation (Groq fast, DeepSeek-R1 deep) |
| **Adjacent** | claude-in-chrome MCP (`~/scripts/sensei_in_chrome/` = Anthropic's official extension v1.0.70) — separately installed + paired ("Browser 1"), tools available but blocked by stale memory |

**Answer to operator's stack question (both):** Manifest V3, vanilla JavaScript, custom Chrome extension talking to a custom Python MCP server through a custom HTTP bridge. Not Playwright, not Puppeteer, not Plasmo. Existing extension already has the primitives the operator specced (see grep hits below).

## Extension-internal architecture (operator's 4-layer expert spec)

Operator's deeper diagnosis after the stack question — these 4 layers live INSIDE the extension (complementary to the 3 ATS layers above, which live at the MCP-server/orchestration level):

### Extension Layer 1 — Multi-Modal Vision Overlay (target painting)
Scan DOM for interactive elements (`input, button, select, [role="button"]`), get `getBoundingClientRect()`, paint numbered badges (e.g. `[Target #42]`) on a canvas overlay. LLM picks by NUMBER, extension translates the number back to exact pixel center. Bullseye accuracy by construction — no coordinate guessing.

### Extension Layer 2 — High-Fidelity Human Mouse Simulation
Native `MouseEvent` API. Real `mousedown → mouseup → click` (and `dblclick`) sequence with microsecond delays. Character-by-character typing via `keydown / keypress / keyup` with random 30-150ms jitter per key. Triggers site-native event handlers (predictive search, validation, etc.) — defeats anti-bot heuristics that reject synthetic teleport-fills.

### Extension Layer 3 — Contextual Field-Length Boundaries
Before LLM generates input text, extract DOM constraints: `maxlength`, `minlength`, `placeholder`, associated `<label>` text. Wrap the LLM prompt with a strict constraint wrapper:
> "You are interacting with Target #42 (Search Bar). Maximum 50 characters. Generate descriptive but strictly under 50."

Stops the "too much / too little text" failure mode.

### Extension Layer 4 — Deterministic State Verification (OODA loop)
- One action at a time (no LLM chaining 5 actions per turn)
- After every click: `MutationObserver` watches the DOM, pauses the LLM
- Screenshot captured only AFTER DOM mutations settle
- Updated visual passed back to LLM for verification before next step

## Extension audit results vs 4-layer spec (evidence-backed)

Read targeted ranges of `content_script.js`. Updated status:

| Layer | Status | Evidence |
|---|---|---|
| **L4 OODA loop** | ✅ **CORRECT** | Lines 79-94: MutationObserver + `scheduleMutationBump` with `setTimeout(...PAGE_STABLE_DEBOUNCE_MS)` debounce. Every action handler ends with `await waitForPageStable(350, 1400)` then `pageContextAsync({waitForStableMs: 150})`. Properly implemented. |
| **L1 vision overlay** | ❌ **MISSING** | 11 ad-hoc `getBoundingClientRect` calls. `ref_N` IDs exist as text in `page_context` but no visible numbered badges. `createElement("div")` only used for ghost-cursor mirror (2297-2313), NOT for target labels. LLM still receives accessibility-tree-style text refs, not painted visual IDs. |
| **L2a mouse sequence** | ❌ **MISSING** | Line 2542 click handler: `el.click()`. Line 2558 dblclick: single `dispatchEvent("dblclick")`. Zero `dispatchEvent("mousedown")` or `mouseup` anywhere in 8,329 lines of extension code. |
| **L2b jitter typing** | ❌ **MISSING** | Full keydown/keypress/keyup sequence (lines 2431-2433) only handles single keys (arrows, Tab). No per-character loop for typing strings. Zero `Math.random()` in extension. Sleep is constant 120ms. |
| **L3 constraint extraction** | ❌ **MOSTLY MISSING** | `placeholder` extracted (line 2124) and bundled into labelText. Zero `maxlength`/`maxLength`/`minlength`/`pattern` references in any extension file. Complete miss on the form-constraint payload. |

## Refactor priority (ranked by leverage × cost)

| Priority | Layer | Why this rank | Estimated effort |
|---|---|---|---|
| **1** | L3 constraints | Cheapest fix, highest signal — add 4-5 attribute reads to the element serializer near line 2124. Immediately stops "too much/too little text" failures across every form, every site. | 1-2 hours |
| **2** | L2a + L2b together | After L3 confirms metadata flows. Use the v1.1 modules (operator-supplied at `/mnt/agents/output/sensei_l2a_*.js` + `sensei_l2b_*.js`) — they correctly handle the 5 known bug classes (duplicate-event from `.click()` after manual mousedown, missing `composed:true` for Shadow DOM, missing clientX/Y, missing shiftKey for caps/symbols, React controlled-component reversion from raw `value=` assignment). | 4-6 hours |
| **3** | L1 vision overlay | Highest accuracy ceiling — eliminates coordinate-guessing. Sits on top of L2/L3 cleanly. Deploy last so any integration issue in L2 is debugged in isolation. | 1-2 days |

Total minimum-viable extension refactor: **~3 days of focused work**, staged across 3 deploys with test gates between each.

## Operator-supplied module package (2026-05-23 second iteration)

Operator brought in v1.1 module files from another agent session at `/mnt/agents/output/`:
- `sensei_l3_constraints.js` — extracts maxLength, minLength, pattern, required, type, min, max, step, autocomplete
- `sensei_l1_vision_overlay.js` — batch scan + paint numbered badges + `getElementByOverlayId()`
- `sensei_l2a_mouse_simulation.js` — pure-dispatchEvent click chain (no `.click()` after), with composed/clientXY/screenXY/pageXY/offsetXY
- `sensei_l2b_jitter_typing.js` — per-char keydown/keypress/input/keyup with shiftKey tracking, execCommand for contentEditable, proper selectionStart/End for React reconciliation
- `INTEGRATION_GUIDE.md` — wiring instructions

These modules correctly fix the 5 framework-desync bug classes that any inline hotfix would re-introduce.

## Critique-validated bug list (the 5 things any patch MUST handle)

1. **No `.click()` after manual mousedown/mouseup** — duplicate event firing
2. **`composed: true` on all dispatched events** — Shadow DOM traversal
3. **Inject clientX/Y/screenX/Y/pageX/Y/offsetX/Y** — framework hit detection
4. **Track shiftKey for capitals + shifted symbols** — modifier-aware listeners
5. **Use execCommand insertText / proper selectionStart for inputs** — React controlled-component reconciliation

A hotfix that misses any of these will re-create the failure modes it's trying to solve.

## Two specific code blocks (for operator review)

**Message listener** (lines 3023-3090) — dispatch hub, all browser work funnels through `SENSEI_EXECUTE_ACTION` → `executeBrowserAction(action)` at line 2374, which switches on `action.kind`. Refactor target for L1 painting + L2 sequencing.

**MutationObserver loop** (lines 79-94 install + per-action `waitForPageStable(350, 1400)` settle) — already correct per spec. Reference implementation for what "right" looks like; the other layers should match this quality bar.

## Why the current stack fails on job applications

| What we have | What we're missing |
|---|---|
| Chrome extension provides real cookies/session/DOM | No ATS detection — every page treated as unique mystery |
| `sensei click(what)` + `sensei fill(where, text)` | No deterministic mapping dictionary — every click is LLM-driven |
| `~/.master_ai_profile` doesn't exist as a structured schema | No source-of-truth JSON for personal data |
| LLM picks every selector by inspecting accessibility tree | LLM is driving 100% — Phase-1 code-fill never runs |

**Result:** every Greenhouse application looks like a new browser-automation puzzle to the LLM. Token cost is high, accuracy is low, the loop happens once per field instead of once per ATS.

## Recommended path forward — two complementary tracks

**Track A — Extension internals (operator's 4-layer spec, partially built):**
Audit `content_script.js` against the 4-layer spec. For each layer where the primitive exists but isn't wired correctly: refactor. Where missing: add. The extension's permissions (`scripting`, `debugger`, `sidePanel`, etc.) already support everything the spec needs. This is a refactor + completion job on existing vanilla JS, not a rewrite.

**Track B — Orchestration layers (operator's 3-layer ATS spec, NOT built):**
Additive on top of the existing sensei_mcp_server + bridge + extension stack:

### Step 1 — `~/.master_ai_profile.json` (Layer 1)
Operator-curated JSON schema with personal data. Structure per operator's example:
```json
{
  "personal": { "first_name": "...", "email": "...", "phone": "...", "linkedin": "..." },
  "experience": [{ "role": "...", "company": "...", "start": "YYYY-MM", "end": "..." }],
  "education": [{ "school": "...", "degree": "...", "year": "..." }],
  "disclosures": { "visa_sponsorship": "...", "gender": "...", "veteran": "...", "race": "..." },
  "resume_url": "file:///home/elijah/.../resume.pdf"
}
```
One-time curation. No LLM involvement after.

### Step 2 — ATS fingerprint + mapping dictionaries (Layer 2)
Add to `~/scripts/` (or new `~/projects/autofill/`):
- `ats_fingerprint.py` — given a DOM dump, return `"greenhouse" | "lever" | "workday" | "unknown"`
- `ats_maps/greenhouse.py`, `lever.py`, `workday.py` — each exports a dict mapping CSS selectors to profile keys (e.g. `'input[name="job_application[first_name]"]': 'personal.first_name'`)

### Step 3 — `autofill_job_form` tool in sensei_mcp_server.py (Layer 3)
New tool on the MCP server. Execution:
1. Call extension to inject content script
2. Content script: read DOM → call fingerprint → look up matching map → for every selector in map, set value from profile → return list of unfilled (custom-question) fields
3. Sensei returns the list to Claude
4. Claude (LLM) generates answers for those 2-3 custom questions using profile context
5. Sensei sends a second injection to fill those targeted fields

The Chrome extension already supports script injection (that's how `fill` works). The change is at the LOGIC layer, not the transport layer.

## Verification path

Two-stage verification, both end-to-end:

**Stage A (cheap, no build):** Confirm claude-in-chrome extension is live (the original verification): `mcp__claude-in-chrome__tabs_context_mcp`. Confirms in-browser context is available either via sensei OR via claude-in-chrome — both have this property.

**Stage B (the real test):** Build Layer 1 + Layer 2 minimal-viable for ONE ATS (recommend Greenhouse — most common, well-documented selectors), expose the autofill tool, and run it against a real Greenhouse application page. Success criterion: standard fields fill in <2 seconds with ZERO LLM tool calls. LLM is only called for the essay questions.

## Verification

1. The diagnostic above must be confirmed by ONE tool call: `mcp__claude-in-chrome__tabs_context_mcp` (read-only, no side effects).
2. If extension responds with valid tab data, the verification succeeds and the stale memory rule is confirmed as the primary missing layer.
3. If extension responds with "not connected", the diagnostic is wrong and investigation pivots to sensei MCP health (screenshot bridge, js_eval reliability).
4. Either result must be reported to the operator before any next action.

## MCP connector context (operator sent the doc twice — incorporating)

The [Claude API MCP connector](https://docs.claude.com/en/docs/claude-code/mcp) (beta `mcp-client-2025-11-20`) defines how external Claude consumers reach MCP servers. Relevance to this investigation:

- **If verification confirms claude-in-chrome works:** that surface is already MCP-accessible from this Claude Code session today. Same model, different transport from sensei. No new infrastructure.
- **If sensei is architecturally broken (screenshot bridge, js_eval, read truncation):** the MCP connector pattern is the architecture for replacing it. Wrap a better browser driver (Playwright, Browser-Use, the claude-in-chrome extension itself) as an MCP server exposing `screenshot`/`click`/`fill`/`read_page` and connect via either local stdio (this Claude Code) or HTTPS URL (claude.ai, API consumers, the operator's other tools).
- **Limit per doc:** server must be publicly exposed HTTPS for the connector. Local stdio MCP works in Claude Code only, not via API connector.

This context informs WHERE the missing layer might be resolvable (use existing extension, replace sensei, or wrap a new tool). It does NOT change the verification step. We still test claude-in-chrome first because that's the cheapest possible confirmation.

## Process gap admitted (operator caught this 2026-05-23)

> "so when I send you out to search how come you don't find this type of information"

The Explore agent for this investigation was briefed for LOCAL-ENVIRONMENT debugging only — read config, audit memories, find forensic traces in `~/`. The operator had to supply the 4 root causes (accessibility tree, UI desync, session traps, native context) himself. Those are widely-documented failure modes in the broader agent-tooling community. Searches I should have included but didn't:

- `WebSearch`: "MCP browser tool accuracy failures", "browser agent click failure modes", "AI form fill desync"
- `mcp__hugging-face__paper_search`: agent browser automation failures, BrowserArena, Browser-Use
- `mcp__hugging-face__hub_repo_search`: browser-use, OpenHands, opencode browser tooling

**Standing rule to add after plan approval:** when asked to "find" or "research," default brief to BOTH (a) local environment audit AND (b) external community knowledge search via WebSearch + HF paper/hub search. Don't pick one without explicit operator constraint. Memory candidate: `feedback_search_local_and_external.md`.

## What this plan IS now

A 3-layer additive build on top of the existing sensei + bridge + Chrome extension stack, following the operator-supplied architecture (Standardized Schema → ATS Fingerprinting → Code-First/LLM-Last). Targets job-application automation accuracy specifically — the workflow the operator references as past success.

## What this plan is NOT

- NOT a proposal to rebuild sensei from scratch (keeps existing stack)
- NOT a proposal to switch transport away from the existing Chrome extension
- NOT a proposal to wrap Playwright/Puppeteer (operator already has a custom extension)
- NOT a proposal to disable the visibility or retry hooks
- NOT scoped to the Field Manual or any other domain — explicitly job applications

## Critical files added if this plan is approved

| New file | Purpose | Owner |
|---|---|---|
| `~/.master_ai_profile.json` | Layer 1 — personal data source of truth | Operator curates |
| `~/scripts/ats_fingerprint.py` | Layer 2 — DOM → ATS identifier | Claude codes |
| `~/scripts/ats_maps/greenhouse.py` | Layer 2 — selector→profile-key dict | Claude codes |
| `~/scripts/ats_maps/lever.py` | Layer 2 — same shape | Claude codes |
| `~/scripts/ats_maps/workday.py` | Layer 2 — same shape | Claude codes |
| `~/scripts/sensei_mcp_server.py` (EDIT) | Layer 3 — add `autofill_job_form` tool | Claude edits |
| `~/projects/autofill/test_greenhouse_e2e.py` | Stage B verification | Claude codes |

## Operator's plan validation + Track B deliverables (2026-05-23 final pass)

Operator validated the plan: Track A priority correct, Track B layering correct, process-gap fix good, verification staging correct, scope discipline correct.

**ATS order confirmed:** Greenhouse → Lever → Workday. Workday deferred to Phase 2 due to tenant-variation in `data-automation-id` selectors.

**Operator addition:** **Telemetry probe in `sensei_bridge.py`** that logs constraint hit/miss rates after Track A L3 deploys. If the LLM still generates text exceeding `maxLength` after receiving the metadata, that's a prompt-engineering problem, not an extension problem. Need data to distinguish.

**Operator-supplied Track B files at `/mnt/agents/output/`:**
- `master_ai_profile.json` — Layer 1 schema (personal, experience, education, skills, disclosures, documents, preferences, pre-written custom answers)
- `ats_fingerprint.py` — Score-based DOM fingerprinting (returns `greenhouse | lever | workday | unknown`, requires 2+ signature hits)
- `ats_maps_greenhouse.py` — Selector→profile-key dict (name, contact, links, work auth, education, experience, disclosures)
- `ats_maps_lever.py` — Minimal viable Lever map
- `ats_maps_workday.py` — Stub; per-tenant validation required
- `LAYER3_AUTOFILL_INTEGRATION.md` — Wiring guide for `sensei_mcp_server.py` with the `autofill_job_form` tool, system prompt update, end-to-end verification script

## Critical integration point (must respect)

**Track A L3 (extension constraints) deploys BEFORE Track B.** If the extension doesn't send `maxlength`/`pattern`/`required` in the page_context payload, the Track B autofill code-fill layer can't enforce them. The two L3 layers (extension constraints + ATS schema) are independent implementations sharing the same principle: hard rules beat LLM guessing.

## Build order (2 hours to first working autofill)

| Step | Action | Time | Owner |
|---|---|---|---|
| 0 | Deploy Track A L3 (constraints inline at line 2124) + telemetry probe in `sensei_bridge.py` | 30 min + 30 min | Claude |
| 1 | Curate `~/.master_ai_profile.json` from template | 30 min | Operator |
| 2 | Drop `ats_fingerprint.py` + `ats_maps/` into `~/scripts/` | 15 min | Claude |
| 3 | Edit `sensei_mcp_server.py` — add `autofill_job_form` tool | 45 min | Claude |
| 4 | Stage A — fingerprint test on static HTML | 5 min | Claude |
| 5 | Stage B — live test on real Greenhouse application page | 15 min | Operator + Claude |
| 6 | Iterate selector accuracy based on results | 30 min | Claude |

## Verification gates

**Gate 1 (must pass before Gate 2):** `fingerprint_ats(greenhouse_html, "https://boards.greenhouse.io/") == "greenhouse"` on a static HTML fixture.

**Gate 2 (must pass before production):** `autofill_job_form(dry_run=True)` on a real Greenhouse page returns `ats_detected: "greenhouse"`, `fields_filled >= 8` (name/email/phone/LinkedIn/resume/experience/education), and `fields_unfilled` contains ONLY essay questions (no standard fields).

**Gate 3 (production):** `autofill_job_form(dry_run=False)`. Standard fields populate in <2 seconds. LLM is called ONLY for custom questions.

## Disclosures handling — Option B (validation-based, not hardcoded allowed-values)

Why Option B over A:
- EEO dropdown strings change as legal-compliance language rotates
- Companies customize default option text per ATS instance
- Fail-graceful beats hardcoded: code attempts fill with operator's preferred string; validation compares against live `<option>` text; on mismatch, ONE LLM call selects closest match from actual available options
- Telemetry probe (from the L3 addition) also logs dropdown match/miss rates — if after ~50 applications a field shows >90% one-string convergence, hardcode it THEN with data

## Workday risk (deferred to Phase 2)

`data-automation-id` selectors vary per tenant. The stub map won't work out of the box on most Workday instances. Phase 1 ships Greenhouse + Lever only. Workday gets per-tenant selector capture (likely a one-tenant-at-a-time approach using sensei's existing DOM read).
