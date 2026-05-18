# APPLY_JOB_SESSION — Master AI skill spec (v1 — 2026-05-17 PM)

A SESSION-scoped skill for processing one batch of job applications.
**The session is the unit, not the site.** ATS-specific handling lives in
per-host adapters INSIDE this skill, not as separate skills.

Operating principle: deterministic code does the orchestration + filter
gating. The local 7B (`qwen2.5:7b` / `master-ai:latest`) does parameter
binding + short free-text generation inside specific steps that need it.
Cloud-frontier escalation only when the recovery path explicitly requests
it. Per Path A (DIY in Master AI's idiom, Elijah 2026-05-17 PM).

## Preconditions

Hard requirements (skill refuses to start if any fails):

- `~/.master_ai_profile.json` exists with `full_name`, `email`, `phone`, `address` populated.
- `~/.master_ai_drive_refs.json` exists with `ai_query_doc` + `applications_log` URLs (chmod 600).
- `~/scripts/skill_runtime.py` is importable.

Soft prerequisites (skill flags as warnings, can proceed without):

- `work_history` in profile.json non-empty — without it, ATS forms that require employment history will need operator input.
- Sensei (`master-ai-ui.service`) and the Chrome extension running — required for any `BROWSER_*` directives. If absent, all steps that drive the browser route to INTERRUPT.

## Parameters

The skill accepts the following session-start params (all optional):

| key                 | shape  | meaning                                                                                |
| ---                 | ---    | ---                                                                                    |
| `candidate_urls`    | list   | URLs to apply to this session. If empty, skill prompts operator at `enumerate_candidate_jobs`. |
| `max_applications`  | int    | Hard cap on submissions this session (default 5).                                      |
| `dry_run`           | bool   | If true, no BROWSER_SUBMIT fires; skill stops at the review-page interrupt every time. |
| `note`              | str    | Free-text note appended to each new applications-log entry.                            |

## Steps

Step machine (deterministic flow; sentinels per `skill_runtime.py`):

1. **load_drive_refs** — Read `~/.master_ai_drive_refs.json`. Populate `state.data["ai_query_url"]` + `state.data["applications_log_url"]`. → next: `load_profile`.
2. **load_profile** — Read `~/.master_ai_profile.json`. Validate required fields; record warnings for missing soft prereqs (work_history empty). → next: `fetch_ai_query_spec`.
3. **fetch_ai_query_spec** — Issue `BROWSER_NAV` to AI Query doc `/mobilebasic` URL, then `BROWSER_READ_PAGE`. Parse rules: dedup list, hard-stop categories, residential filter rule, account-creation hard stop, sensitive-field gate. Store in `state.data["ai_query_rules"]`. → next: `fetch_applications_log`.
4. **fetch_applications_log** — Issue `BROWSER_NAV` to applications-log `/mobilebasic` URL, then `BROWSER_READ_PAGE`. Parse: DO-NOT-RE-APPLY list, applied entries, in-progress entries, total counts. Store in `state.data["applications_log"]`. → next: `reconcile_inbox`.
5. **reconcile_inbox** — Scan Gmail (msmtp/IMAP, if wired) + AOL inboxes for confirmation emails matching every entry marked `Submitted`. Update local cache; flag discrepancies. INTERRUPT if any "Submitted but no confirmation" entries; operator decides (mark as Failed, mark as Verified Manually, leave open). → next: `enumerate_candidate_jobs`.
6. **enumerate_candidate_jobs** — Filter `candidate_urls` against `applications_log["dedup_list"]` + `ai_query_rules["hard_stops"]` + residential filter. If `len(candidate_urls) == 0` after params, INTERRUPT for operator-supplied URLs. Output: `state.data["queue"]` = pre-filtered list. → next: `apply_one_job`.
7. **apply_one_job** — Pop one URL from `queue`. Dispatch to per-host adapter (`adapter_indeed` / `adapter_ziprecruiter` / `adapter_workday` / `adapter_greenhouse` / `adapter_lever` / `adapter_ashby` / `adapter_icims` / `adapter_custom`). Adapter handles: load page, classify form pages, fill from profile, generate free-text answers via local 7B, INTERRUPT before any `BROWSER_SUBMIT`. After operator approves → submit → wait → detect confirmation. Append result to `state.artifacts["applications"]`. → next: `loop_or_done`.
8. **loop_or_done** — If `queue` is non-empty AND `state.data["submitted_count"] < params.max_applications`, → `apply_one_job`. Else → `log_session`.
9. **log_session** — Emit new SESSION block to the applications log via `BROWSER_NAV` + writeable Drive primitive (TBD — may interrupt for operator copy/paste in v1 if Drive write primitive isn't wired yet). Includes: date, list of submitted/failed entries, notes, total count. → END.

Recovery routes (per-step `recovery_next` for retries-exhausted):

- `fetch_ai_query_spec` recovery → INTERRUPT (operator pastes doc text manually).
- `fetch_applications_log` recovery → INTERRUPT (operator pastes log).
- `apply_one_job` recovery → `loop_or_done` (skip this URL, move to next).
- All other steps recovery → ABORT.

## Postconditions

Skill is `done` only if:

- All `apply_one_job` iterations exited with either `applied`, `skipped_dedup`, `skipped_hard_stop`, or `failed_<error_code>` recorded.
- `log_session` step completed (either via Drive write OR operator confirmed manual update).
- Session JSON at `~/.master_ai_skills/apply-job-session/sessions/<session_id>.json` shows `done: true`.

## Recovery / failure-mode handling

ATS-side failures the skill must handle gracefully (no retry-spin):

- iCIMS server error → log `failed: icims_server`, move on.
- Aerotek state-dropdown freeze → log `failed: aerotek_state_dropdown`, move on.
- Paradox / Allie misflag as current employee → log `failed: paradox_employee_misflag`, move on.
- Workday "session expired" mid-flow → log `failed: workday_session_expired`, move on (do NOT auto-re-login).
- CAPTCHA / 2FA / "verify you're human" → INTERRUPT, operator handles.

Account-creation hard stop: any page that requires creating a NEW account (signup form, "create profile" page) → INTERRUPT regardless of mode. Account creation is operator-only per AI Query rules.

Sensitive-field gate: any field matching password / SSN / DOB / cc_number / cvv / routing / account → NEVER auto-fill, emit `NEEDS_INPUT: sensitive_fill :: <field_label>`.

## Per-host adapters (v1 stubs; flesh out during 12-hour push)

Each adapter is a Python function in `recipe.py` with signature:
```python
def adapter_<host>(state: SkillState, url: str, profile: dict, rules: dict) -> dict
```
Returns: `{"outcome": "applied"|"skipped"|"failed_<reason>"|"interrupt", "details": dict}`.

v1 adapters: ALL return `{"outcome": "interrupt", "details": {"reason": f"adapter_{host}_not_implemented"}}`. The 12-hour push fills in real fill/submit/verify logic per ATS.

Host detection from URL host portion: `indeed.com`, `ziprecruiter.com`, `myworkdayjobs.com` / `wd*.myworkdayjobs.com`, `boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `careers-*.icims.com`, fallback `adapter_custom`.

## Filter rules (encoded from AI Query doc + applications log practice)

Hard stops the skill enforces before any keystroke:

- **Apartment / multifamily / resident-access maintenance** — OUT.
- **Companies on DO-NOT-RE-APPLY list** — OUT.
- **Staffing-agency skip list** (currently Express Employment per work-history-stale flag) — OUT until operator unflags.

Soft preferences (skill applies but doesn't block):

- Commercial HVAC / facilities preferred over residential.
- Residential install / service / new-construction OK.
- Residential foreclosure / vacant-portfolio OK.

Special cases:

- **Indeed Apply:** wraps employer ATS. Employer-name field is filled POST-submit from Indeed account history, NOT at submit time. `adapter_indeed` handles deferred employer extraction.

## Wisdom-compounds notes (accumulation mechanisms)

The skill is designed to LEARN per session:

- `state.data["ai_query_rules"]` and `state.data["applications_log"]` get cached to `~/.master_ai_skills/apply-job-session/cache/` after each session; subsequent sessions read from cache + delta-fetch if doc mtime changed.
- Successful free-text answers (cover-letter blurbs, "why this company" responses) get appended to `~/.master_ai_skills/apply-job-session/answer_library.jsonl` keyed by question-shape hash. Next session's free-text generation step consults the library first before calling the LLM.
- Recognized ATSes — when `adapter_custom` succeeds against a previously-unrecognized host, the host gets a stub entry in `~/.master_ai_skills/apply-job-session/host_registry.json`. Over time, custom careers pages get classified.

These accumulation files are NOT in git; they live in `~/.master_ai_skills/apply-job-session/` and grow over use.

## Out of scope for v1

- Drive WRITE primitive — log_session may interrupt for manual copy/paste until the writeable Drive endpoint is wired.
- Multi-account inbox reconciliation requires mbsync (not yet installed; parked).
- LinkedIn / Glassdoor / Dice — not in v1 host list.
- Cover-letter generation as a separate step — v1 generates inline during `adapter_*` when an ATS form has the field.
- Automatic Express Employment retry once work_history updates — staffing-agency skip list is operator-managed.

## Verification (smoke test)

```
python3 ~/scripts/skill_runtime.py apply-job-session '{"candidate_urls": [], "dry_run": true}'
```

Expected output for v1 stub:

- Preconditions pass.
- Steps 1-2 (load_drive_refs, load_profile) complete successfully.
- Step 3 (fetch_ai_query_spec) INTERRUPTS at v1 because the `BROWSER_NAV` directive emission is the integration point that Codex's master_ai.py update will wire.
- Session JSON written to `~/.master_ai_skills/apply-job-session/sessions/<session_id>.json` with `current_step: __interrupt__` and `interrupt_reason: "browser_directive_emission_not_wired_in_v1"`.

When Codex wires the `RUN_SKILL: <name>` directive parser in master_ai.py + the skill-runtime-emits-BROWSER_directives integration, the skill advances past step 3 and starts driving Chrome end-to-end.
