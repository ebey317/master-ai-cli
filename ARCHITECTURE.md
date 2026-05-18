# Master AI — Architecture

## 0. Purpose

This document explains the shape of master_ai and why it's shaped that way. It's written for the next person who opens this repo — including future-me — who needs to understand the system before changing it. It's not a marketing page and it's not an exhaustive API reference. It's the design diary that captures every architectural decision made up through 2026-05-18, with the reasoning each time, so a contributor doesn't have to dig through commit messages to learn why a slot exists or why a gate runs in a specific order.

If you're reading this to figure out whether to refactor something — read the relevant section before you change it. Most of these shapes have a reason behind them; some of those reasons are documented as regression tests in `skills/apply-job-session/test_recipe.py` and a "fix" that doesn't account for them gets caught.

---

## 1. The two-piece architecture

Master AI ships as two pieces installed on the customer's machine.

**The brain (master_ai)** is a local Python application. It runs the local model via Ollama by default, with optional cloud escalation per message (Groq / OpenRouter / Gemini, BYOK). The installer creates two terminal commands — `master` for the menu portal and `sensei` for the agent — and adds `~/.local/bin` to PATH automatically. Sensei is the tmux-hosted terminal agent. Pupil is the browser UI.

**The hands (chrome_extension at `~/scripts/sensei_extension/`)** is a Chrome MV3 extension. Its `content_script.js` dispatches `BROWSER_NAV` / `BROWSER_FILL` / `BROWSER_CLICK` / `BROWSER_UPLOAD` / `BROWSER_READ_PAGE` directives against whatever tab the user is on. Native messaging connects it back to master_ai over localhost. The extension lives in the user's already-logged-in Chrome — that's the whole point. We don't ask the user to hand over their session; we operate inside the session they already have.

The brain emits structured directives. The hands execute them. The two are linked by MCP (Model Context Protocol) inside the customer install. Nothing else ships. Codex / Replit / Codespaces / claude-in-chrome / browser-side Claude — all of those are development tooling, useful for building the product, never shipped to the customer.

> **Design principle.** *The code is the extension. The linking layer between them is the product.* Two pieces. One architecture. Local-first by default.

---

## 2. The executor framework

The executor is the part that decides, for every field on every form, what to do — auto-fill, fill-and-confirm, show candidates, stop and ask, or refuse outright. It lives in `skills/apply-job-session/recipe.py` and is shared across every per-ATS adapter the apply-job-session skill dispatches to.

Think of the executor like a careful assistant filling out a form on your behalf at the kitchen table. For every line, the assistant pauses and asks two questions: should I act on this field at all right now? and if I do, how sure am I and what's the handoff?

**Two decisions per field, not one.** Conflating "should I act" with "how confident am I" produces matrices with dead cells. Keep them separate and the logic stays legible.

**Decision 1 — the sensitivity gate.** Every field has a sensitivity tier: `none`, `personal`, `financial`, `government_id`. The mapping from coarse semantic field roles (`name_first`, `email`, `phone`, `ssn`, `bank_routing`, `passport_number`, etc.) to sensitivity tiers lives in `FIELD_ROLE_TO_SENSITIVITY` — one central dict, 28+ roles, four tiers. Adapter configs only name the role; the executor derives sensitivity at runtime so the rules live in one place.

Fields at `financial` or `government_id` skip the rest of the ladder entirely and emit `BRANCH_REFUSE_SENSITIVE` regardless of match confidence. A perfect (1.0) match on an SSN field still refuses. This is non-negotiable and there's a named regression test in place — a future contributor who relaxes the gate gets caught by `test_above_personal_never_auto_fills_even_at_full_confidence`.

**Decision 2 — the four-tier ladder.** For fields below the sensitivity ceiling, the executor uses match confidence to pick a branch:

- **Very sure** (`confidence >= 0.9`) → `auto_fill_flag` — fill the field, log the fill, surface it in the end-of-session summary
- **Somewhat sure** (`0.7 ≤ confidence < 0.9`) → `fill_with_confirm` — fill and pause for operator confirmation
- **Guess** (`0.3 ≤ confidence < 0.7`) → `disambiguate` — present the top candidates, let the operator pick
- **No idea** (`confidence < 0.3`) → `stop_and_ask` — empty value, hand the keyboard to the operator

**The current_value gate.** Before the ladder runs, if the field already has a value (operator typed it manually, or a previous fill landed there), the executor defaults to `fill_with_confirm` rather than overwriting. Never clobber existing values without operator confirmation.

---

## 3. Audit log and health surface

Every executor decision writes exactly one line to an append-only JSONL audit log at `~/.master_ai_skills/apply-job-session/audit_log.jsonl`. Every branch logs — `auto_fill_flag`, `fill_with_confirm`, `disambiguate`, `stop_and_ask`, `refuse_sensitive`. There are no silent decisions.

> **Design principle.** A safe tool that doesn't log its safety decisions is a silent tool. A silent tool is worse than a chatty unsafe one because you can't see what it did.

Each entry carries `{ts, domain, page_url, step_id, field_ref, field_label_visible, field_type, sensitivity, branch, match_confidence, match_signal_source, profile_field_used, value_recorded, value_redacted_reason, operator_action, latency_ms}`. Timestamps are ISO 8601 UTC with microsecond precision so audit lines from the same second stay distinguishable.

**Value redaction policy by sensitivity tier:**
- `none` + non-freeform field → record the actual value
- `personal` → record a fingerprint (last 4 of phone, domain of email) — never the full value
- `financial` / `government_id` / freeform → record nothing; populate `value_redacted_reason`

**The `no_value` reason.** A separate redaction reason exists for interrupt branches that genuinely had no value to record (`stop_and_ask`, `disambiguate`). It's distinct from `sensitivity:government_id` because operationally they mean different things: one is "we honestly had no match," the other is "we refused on purpose." Sensitivity wins over `no_value` in ordering — a refused government_id reads as sensitivity-refusal, not no-value.

**Audit log health surface.** When a write fails (disk full, permission denied, parent directory unwriteable), the executor stays functional. The failure populates a module-level `audit_log_health = {healthy, first_failure_since, last_error}` and the executor returns its decision normally. The runtime UI / operator watches the health flag and surfaces unhealthy state separately. The executor does NOT raise — degraded conditions can't crash the agent — and does NOT retry — single attempt, surface, move on.

Once unhealthy, the flag stays unhealthy until explicit reset. No silent self-heal. A flag that auto-clears after a successful write masks real damage the operator never sees.

---

## 4. PageContext, PageSignals, and the read_form phase

The producer for form-shape information is `page_signals_from_context`. It takes a `PageContext` (raw page text from BROWSER_READ_PAGE plus light structured metadata) and an optional `previous_context` (for stability checks across two reads) and returns a `PageSignals` dataclass with eight slots: `step_index`, `total_steps`, `is_submit_step`, `is_hydrated`, `has_blocking_errors`, `validation_errors`, `continue_button_present`, `continue_button_enabled`.

The schema landed at 8 slots after starting at 11. The dropped trio (`step_progress_source`, `page_url`, `page_title`) had no consumers in the immediate cycles — they were "schema stability" for stability's sake. Optional fields can be added back when their consumers land without breaking callers.

> **Design principle.** Schema-shape stability matters when consumers couple to the shape. Don't carry slots nobody reads.

The producer is a pure function. The caller decides whether to do two reads for a stability check (and pass `previous_context`) or to trust a single read. Single-read is the primary path; two-read is the more confident path. Keeping the function pure means the caller owns the read budget instead of the producer.

**read_form_current_step** is the phase function that drives reads and produces FormDescriptorRecords. It has three branches at entry:

1. **First invocation on this step** — no read has happened. Emit `BROWSER_READ_PAGE`, set `_initial_read_dispatched=True`, interrupt with reason `awaiting_initial_read`. The next re-entry consumes the read result.

2. **Read was dispatched but no result** — the directive didn't fire. This is a directive-execution failure, not a hydration failure. Interrupt with `read_directive_failed`. It does NOT consume a retry slot; the retry budget is reserved for actual hydration.

3. **Read result present** — run `page_signals_from_context`. Hydrated → write a FormDescriptorRecord into `_form_descriptors_current_step` and transition to `fill_form_current_step`. Not hydrated → increment the retry counter; if budget exhausted (`_READ_FORM_MAX_RETRIES = 3`) interrupt with `hydration_failed_after_3_attempts`, else emit `BROWSER_WAIT(500) + BROWSER_READ_PAGE` and stay in the phase.

The 3-attempt cap is a named regression test (`test_read_form_returns_hydration_failed_when_retry_budget_exhausted`). Unbounded retry on a stuck page burns the session indefinitely; that's the property the regression locks.

---

## 5. Task model v0

A task is a single unit of operator-intent. The v0 task abstraction has:
- A lifecycle: `SPAWNED` → `RESOLVING_TARGET` → `RUNNING` → `TERMINATED`
- Four terminated reasons: `APPLIED`, `SKIPPED`, `INTERRUPTED`, `FAILED`
- A `Task` dataclass with `task_id`, `task_type` ("apply_one_job" in v0; extensible), `state`, `target`, `params`, `artifacts`, `terminated_reason`, `spawned_at`

The dispatcher `task_dispatch(task, phase_fn)` advances a task by one phase call and maps the phase's `outcome` to the task's state transition. `interrupt` keeps the task in `RUNNING` (operator pause, not termination). Unknown outcomes fail loud — terminated as `FAILED` with a `_dispatcher_note` — rather than letting state-machine drift go silent.

V0 deliberately does NOT include persistence, multi-task scheduling, or cross-task dependencies. Persistence is the longest-lived piece and the last to land; it depends on having something worth persisting, and designing it first risks designing for the wrong shape.

Re-entering a terminated task raises `RuntimeError`. Re-running a task that already finished would silently restart it, and silent restarts on irreversible flows (like a real apply) are the kind of bug that's invisible until it's already happened.

---

## 6. The atss/ pattern

Per-ATS configs live in `skills/apply-job-session/atss/` — one file per ATS (`indeed_smart_apply.py`, future siblings for ZipRecruiter / Workday / LinkedIn / Glassdoor / Honest Jobs). Each file carries selectors captured by walking a real flow with a human in the loop, plus a self-documenting safety clause at the top.

The safety clause says explicitly: these configs do NOT constitute permission or capability for autonomous, unattended submission. The runtime that reads them is required to keep human-in-loop on every irreversible branch — CAPTCHA, sensitive fills, final submit. The clause lives in the file itself, not just in operator memory, so a future contributor who picks up the codebase reads the boundary before touching the selectors.

Selectors are captured one ATS at a time, one real flow at a time. Serial captures across many listings get accounts flagged and cross from documenting-one-flow into automated reconnaissance. The pattern stays one human-in-loop walkthrough per session.

---

## 7. What's queued

These are the architectural pieces designed but not yet shipped:

- **Cycle 3 of page_signals**: the fill_form match loop. Gate-then-match logic: freshness check (does the FormDescriptorRecord still match the current step?), errors gate (interrupt on validation), submit-step gate (route to submit_gate phase), then iterate descriptors through `_executor_decide`. The descriptors themselves still need real DOM extraction from live captures — the v0 stub holds one synthetic descriptor marked for replacement.

- **Audit log follow-ups**: `last_failure_ts` and `failures_count` slots in `audit_log_health` for richer unhealthy-window reporting. Schema is one Optional field each; no callers break.

- **MCP per-domain consent piece**: claude-in-chrome's allowlist UI doesn't surface popups reliably; the documented workaround is a same-origin JS-redirect bypass from a permitted tab. A real fix would land per-domain consent in a place the user can flip with one click.

- **Beast Mode multi-ATS captures**: ZipRecruiter Quick Apply, LinkedIn Easy Apply, Glassdoor Easy Apply, Workday (`*.myworkdayjobs.com`), Honest Jobs. Each gets its own `atss/` config with the same shape as `indeed_smart_apply.py`. Captures are operator-led, one ATS per session.

- **Task model expansion**: cross-tab routing (how does the task know which tabs belong to it), persistence layer (saving and restoring task state across sessions), priority model (respecting operator constraints on certain tabs' loudness), email channel, document channel. The shape is sketched; the implementations come when there's a use case driving each.

- **Read-side filtering** (the "constraint-driven recon" reversal of form-fill): same descriptor + executor pattern, applied to gating options instead of filling fields. Job criteria, shopping filters, government-doc requirements — all the same engine running in reverse direction.

---

*This architecture was designed iteratively through dialogue with Anthropic's Claude — both via the terminal-side Claude Code and the browser-side Claude.ai panel — across multiple work sessions. Every decision in this document was made by the operator; the dialogue was the medium, not the authority. When something here looks wrong, fix it with the same standard: read why it's shaped this way, then decide whether the why still holds.*
