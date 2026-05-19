# Cycle 3 — fill_form match loop with real DOM extraction

**Status:** Design 2026-05-18 (late session). Implementation deferred to focused fresh-token session, estimated 4-6 hours.

**Why this matters:** The reason forms don't actually fill end-to-end is named precisely in [ARCHITECTURE.md §7](../../ARCHITECTURE.md): the v0 fill_form stub holds **one synthetic descriptor marked for replacement**. The match loop logic exists in the executor framework, but it's iterating over fake data instead of real form fields parsed from live page captures.

---

## The two missing pieces

### Piece A — `_extract_form_descriptors(page_context)`

Parse `BROWSER_READ_PAGE` output (a11y tree + visible text + selectors) into a list of `FormDescriptorRecord` dataclasses, one per form field on the current step.

Input shape from extension (per existing BROWSER_READ_PAGE returns):
```json
{
  "url": "...",
  "title": "...",
  "a11y_tree": [...],          // accessibility tree nodes
  "visible_text": "...",
  "selectors": [{"role":"textbox", "name":"Email", "selector":"input[name=email]"}, ...],
  "frames": [...]              // iframe summaries
}
```

Output shape (the dataclass that already exists in recipe.py):
```python
FormDescriptorRecord(
  field_ref="input[name=email]",
  field_label_visible="Email Address *",
  field_type="email",
  required=True,
  current_value="",
  semantic_role="email",         # mapped via FIELD_ROLE_TO_SENSITIVITY
  sensitivity="personal",        # derived from role
)
```

The extraction needs to:
1. Walk the a11y tree, filter to `role in {textbox, combobox, checkbox, radio, ...}` 
2. For each, pull the visible label (sibling text, `aria-label`, `aria-labelledby`, `placeholder` in that order)
3. Detect required-ness (`aria-required`, `required` attribute, or asterisk in label)
4. Detect type (input type, role, or label heuristics: "email" → email, "phone" → phone, etc.)
5. Map label → semantic role via fuzzy match against `FIELD_ROLE_TO_SENSITIVITY` keys
6. Derive sensitivity from the role

**Estimated:** ~250 lines + unit tests.

### Piece B — `fill_form_current_step` real-mode logic

Currently a stub. Real-mode flow per [ARCHITECTURE.md §7 queued]:

1. **Freshness check** — does the FormDescriptorRecord set still match the current page? If not (URL changed, hydration changed), re-read.
2. **Errors gate** — if `has_blocking_errors` in PageSignals, interrupt with reason `validation_errors_present`; let operator address before continuing.
3. **Submit-step gate** — if `is_submit_step`, route to `submit_gate_phase` (separate phase, requires explicit operator confirm for irreversible action).
4. **Iterate descriptors through `_executor_decide`** — for each FormDescriptorRecord, call the existing executor (sensitivity gate + 4-tier match-confidence ladder) to get a decision: `auto_fill_flag` / `fill_with_confirm` / `disambiguate` / `stop_and_ask` / `refuse_sensitive`.
5. **Emit directives** — for each decision, emit the right `BROWSER_FILL: selector :: value` (or `BROWSER_CLICK:` for checkboxes/radios, or `interrupt` for stop_and_ask).
6. **Log every decision** — append to `~/.master_ai_skills/apply-job-session/audit_log.jsonl` (existing health-surface pattern).

**Estimated:** ~200 lines + integration tests.

---

## What to clone vs build from scratch

**Don't clone code — clone the ARCHITECTURAL PATTERN** from these:

- **browser-use** (github.com/browser-use/browser-use) — MIT-licensed Python library, autonomous browser agent. Uses LLM-driven form decisions. The pattern worth copying: their `Agent → Browser → DomService → ElementTree` flow, where the LLM gets a structured DOM representation and emits actions. Not the code (different runtime), the shape.
- **Skyvern** (github.com/Skyvern-AI/skyvern) — open-source LLM browser agent for form filling specifically. Look at how they handle: dynamic field detection, conditional flows (e.g., "if employer has YES, show 'previous role'" branching), multi-step wizards.

**AVOID copying from:**
- **AIHawk / Jobs_Applier_AI_Agent** — per memory `[[project_secretary_pivot_actual_product]]`, this hit copyright/ToS pressure. The technical patterns are visible but legally hostile.

The PRINCIPLE: read their approaches, name the patterns, write FRESH code that fits master_ai.py's existing executor framework. Don't fork.

---

## Execution sequence for the next focused session

**Estimated 4-6 hours total, one continuous session.**

1. **Capture a live Indeed Smart Apply form** (30 min)
   - Operator drives Sensei to a real apply page
   - `BROWSER_READ_PAGE` once, capture the JSON to `~/scripts/skills/apply-job-session/captures/indeed_step1.json`
   - Repeat for steps 2-4 of the same flow
   - Sanitize: redact any personal info before commit

2. **Implement `_extract_form_descriptors`** (90 min)
   - Write the parser against the captured JSON
   - Unit tests using captured fixtures
   - Smoke test: extracted descriptors should have right labels, types, required-ness

3. **Implement `fill_form_current_step` real-mode** (90 min)
   - Wire the 6-step flow above
   - Integration tests using the executor + captured fixtures
   - Verify audit log writes for each decision

4. **End-to-end live test** (60 min)
   - Run against a real Indeed apply (operator at keyboard for sensitive fills)
   - Watch the executor decisions log in real time
   - Iterate on label-matching where confidence is too low or wrong

5. **Commit + push + memory update** (15 min)
   - Single commit: `feat(apply-job-session): cycle 3 — fill_form match loop with real DOM extraction`
   - Memory: add a `project_apply_job_session_cycle3_live.md` with what worked vs what needs cycle 4

---

## What this unlocks (the practical answer)

Once Cycle 3 lands, Sensei can:
- Read a real form
- Decide what to fill (within safety gates)
- Emit fills as `BROWSER_FILL` directives
- Verify via re-read
- Move to the next step

That's the "form filling that actually works end-to-end" the operator named tonight. Until Cycle 3 lands, the executor framework is real but is iterating over a synthetic descriptor — same as a brain with no eyes.

## Related

- [[project_master_ai_architecture]] §7 (Cycle 3 named as queued)
- [[project_secretary_pivot_actual_product]] (the WHY — apply-job is one capability of the secretary, not the product itself)
- [[project_anthropic_brand_ambassador_angle]] (this work product also strengthens the partnership pitch — concrete real-form fills become a demo)
- [[feedback_job_application_pipeline]] (the operator-facing flow: profile → Simplify → NEEDS_PROFILE_FIELD → batch=5)
