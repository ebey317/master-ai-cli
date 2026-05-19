"""
APPLY_JOB_SESSION skill recipe (v1 — 2026-05-17 PM).

Implements the STEPS list per the skill_runtime contract. v1 is a
structural stub: the deterministic orchestration (file I/O, filter
evaluation, dedup, branching) is FULLY WORKING; the steps that need
master_ai.py BROWSER_* integration emit INTERRUPT with a clear reason
so the 12-hour push can wire them next without rewriting structure.

Read SKILL.md in this same directory for the full spec.

Per Path A (Elijah 2026-05-17 PM): no langgraph import, no browser-use
import, no langchain import. Stdlib + skill_runtime only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Import skill_runtime from the canonical location. The runtime adds itself
# to sys.path when invoked via `python3 ~/scripts/skill_runtime.py`, but if
# this recipe is loaded by a different harness we ensure the import works.
_SCRIPTS = Path(os.path.expanduser("~/scripts"))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from skill_runtime import (  # noqa: E402
    Step,
    SkillState,
    END,
    ABORT,
    INTERRUPT,
    PreconditionFailed,
)


# ─── Constants ──────────────────────────────────────────────────────

PROFILE_PATH = Path(os.path.expanduser("~/.master_ai_profile.json"))
DRIVE_REFS_PATH = Path(os.path.expanduser("~/.master_ai_drive_refs.json"))
SKILL_HOME = Path(os.path.expanduser("~/.master_ai_skills/apply-job-session"))
CACHE_DIR = SKILL_HOME / "cache"
ANSWER_LIBRARY = SKILL_HOME / "answer_library.jsonl"
HOST_REGISTRY = SKILL_HOME / "host_registry.json"

# Default cap on submissions per session if params don't override.
DEFAULT_MAX_APPLICATIONS = 5

# Per-host adapter registry (host suffix → adapter function name). The skill
# `apply_one_job` step matches a candidate URL's host against this dict and
# dispatches to the named function. Suffix-based so subdomains work
# (e.g. `wd1.myworkdayjobs.com` matches `myworkdayjobs.com`).
HOST_ADAPTERS = {
    "indeed.com": "adapter_indeed",
    "ziprecruiter.com": "adapter_ziprecruiter",
    "myworkdayjobs.com": "adapter_workday",
    "boards.greenhouse.io": "adapter_greenhouse",
    "jobs.lever.co": "adapter_lever",
    "jobs.ashbyhq.com": "adapter_ashby",
    "icims.com": "adapter_icims",
}


# ─── Executor framework (browser-Claude design, 2026-05-18 dialogue) ──
#
# Plain-language model: the executor is "a careful assistant filling out a
# form on your behalf at the kitchen table." For every field it asks two
# questions: (1) should I act on this field at all right now? and (2) if
# I do, how sure am I and what's the handoff? Each decision emits one
# audit log entry, no exceptions.

SENSITIVITY_NONE = "none"                    # innocuous (name, work history)
SENSITIVITY_PERSONAL = "personal"            # contact info (email, phone, address)
SENSITIVITY_FINANCIAL = "financial"          # bank routing, account numbers, salary
SENSITIVITY_GOVERNMENT_ID = "government_id"  # SSN, passport, immigration numbers

_SENSITIVITY_ORDER = [
    SENSITIVITY_NONE,
    SENSITIVITY_PERSONAL,
    SENSITIVITY_FINANCIAL,
    SENSITIVITY_GOVERNMENT_ID,
]

# Four-tier escalation ladder (decision 2 — confidence + handoff).
BRANCH_AUTO_FILL_FLAG = "auto_fill_flag"        # very-sure: fill + log
BRANCH_FILL_WITH_CONFIRM = "fill_with_confirm"  # somewhat-sure: fill + ask
BRANCH_DISAMBIGUATE = "disambiguate"            # guess: show candidates, pick
BRANCH_STOP_AND_ASK = "stop_and_ask"            # no idea: stop for keypress

# Pre-ladder gate (decision 1 — sensitivity short-circuit).
BRANCH_REFUSE_SENSITIVE = "refuse_sensitive"    # human keypress, no auto-fill

# Confidence thresholds for non-sensitive fields.
AUTO_FILL_CONFIDENCE_THRESHOLD = 0.9
FILL_WITH_CONFIRM_THRESHOLD = 0.7
DISAMBIGUATE_THRESHOLD = 0.3

# Interrupt resume-token TTLs.
INTERRUPT_TTL_MS_NORMAL = 5 * 60 * 1000     # 5 min for ordinary fields
INTERRUPT_TTL_MS_SENSITIVE = 15 * 60 * 1000  # 15 min — operator may walk to get passport

AUDIT_LOG_PATH = SKILL_HOME / "audit_log.jsonl"

# Health surface for the audit log write itself. The executor stays
# functional under degraded conditions (disk full, permission denied,
# parent-dir unwriteable); the failure is surfaced via this dict for
# the UI / operator to act on. Stays False until explicitly reset —
# a silent self-heal would mask real damage.
#
# Slots:
#   healthy              — False once any write fails
#   first_failure_since  — ISO 8601 of the FIRST failure in the unhealthy
#                          window; not overwritten by later failures
#   last_failure_ts      — ISO 8601 of the MOST RECENT failure; overwritten
#                          on every failure for unambiguous semantics
#   failures_count       — total failures in the current unhealthy window;
#                          reset only on explicit clear
#   last_error           — truncated, home-redacted exception string
#
# first_failure_since + last_failure_ts together give the operator the
# duration of the unhealthy window. failures_count gives the magnitude.
audit_log_health = {
    "healthy": True,
    "first_failure_since": None,
    "last_failure_ts": None,
    "failures_count": 0,
    "last_error": None,
}


def _audit_log_health_record_failure(exc: Exception) -> None:
    """Set audit_log_health into unhealthy state. `first_failure_since` is
    pinned on the first failure in a window; `last_failure_ts` is overwritten
    on every failure. `failures_count` increments every failure. All three
    reset only via explicit clear (see _reset_framework_state_for_tests or
    operator UI action)."""
    import datetime
    msg = str(exc)
    if len(msg) > 500:
        msg = msg[:497] + "..."
    home = os.path.expanduser("~")
    if home and home in msg:
        msg = msg.replace(home, "~")
    now_iso = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
    )
    audit_log_health["healthy"] = False
    audit_log_health["last_error"] = msg
    audit_log_health["last_failure_ts"] = now_iso
    audit_log_health["failures_count"] = audit_log_health.get("failures_count", 0) + 1
    if audit_log_health["first_failure_since"] is None:
        audit_log_health["first_failure_since"] = now_iso


def _reset_framework_state_for_tests() -> None:
    """Reset every module-level mutable in the executor framework to a
    known-good state. Tests MUST call this in setUp AND tearDown so cross-
    test state bleed (a write-failure test poisoning a happy-path test) is
    impossible.

    When you add new module-level mutable state, update this function. The
    convention is enforced by browser-Claude design review 2026-05-18 — the
    helper is the place to look when wiring a new safety test."""
    audit_log_health["healthy"] = True
    audit_log_health["first_failure_since"] = None
    audit_log_health["last_failure_ts"] = None
    audit_log_health["failures_count"] = 0
    audit_log_health["last_error"] = None
    # AUDIT_LOG_PATH is a constant (never mutated in production paths).
    # Tests pass audit_path explicitly to _executor_decide / _audit_log
    # rather than monkey-patching the module-level path.


def _audit_log(entry: dict, path=None) -> None:
    """Append-only audit log. One JSON line per executor decision. `path`
    defaults to the module-level AUDIT_LOG_PATH; pass explicitly for test
    isolation (chosen over monkey-patching per browser-Claude design review
    2026-05-18).

    Write failures (disk full, permission denied, parent-dir unwriteable)
    are caught and surfaced via `audit_log_health`. The function does NOT
    raise — the executor must stay functional under degraded conditions —
    and does NOT retry. Surface the state, move on."""
    import datetime
    log_path = Path(path) if path else AUDIT_LOG_PATH
    ts_iso = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
    )
    entry_full = {"ts": ts_iso, **entry}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(entry_full) + "\n")
    except Exception as e:
        _audit_log_health_record_failure(e)


def _fingerprint(value, field_type=None) -> str:
    """Short fingerprint of a personal-tier value: domain of email, last 4
    of phone digits, otherwise length. Never returns full value."""
    s = str(value or "")
    if "@" in s:
        return "..." + s.split("@", 1)[1]
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 4:
        return f"...{digits[-4:]}"
    return f"len:{len(s)}"


def _audit_value(value, sensitivity: str, field_type=None):
    """Per-sensitivity redaction policy for `value_recorded`."""
    if value is None:
        return None
    if sensitivity == SENSITIVITY_NONE and field_type != "textarea":
        return value
    if sensitivity == SENSITIVITY_PERSONAL:
        return _fingerprint(value, field_type)
    return None  # financial / government_id / freeform → never record


def _audit_redact_reason(sensitivity: str, field_type=None, has_value: bool = True):
    """Reason `value_recorded` is empty/redacted. Sensitivity wins over
    `no_value` so a refused government_id reads as sensitivity-refusal in the
    audit trail, not "we had nothing to fill." `no_value` is reserved for
    none-tier interrupt branches (stop_and_ask, disambiguate) where the
    executor honestly had no match to suggest."""
    if sensitivity != SENSITIVITY_NONE:
        return f"sensitivity:{sensitivity}"
    if not has_value:
        return "no_value"
    if field_type == "textarea":
        return "freeform_field"
    return None


def _executor_decide(field_descriptor: dict, match: dict, audit_path=None) -> dict:
    """Apply the executor's two-decision logic to one field. Returns a
    `decision` dict with `branch` and supporting payload. Always emits one
    audit log entry as a side effect (no silent decisions).

    field_descriptor expected keys: ref, label{visible,aria,placeholder,legend},
    name, id, field_type, required, options, current_value, editability,
    sensitivity.

    match expected keys: confidence (float 0-1), value, source (the matching
    signal — label-similarity, name-attr, etc.), profile_field, candidates."""
    sensitivity = field_descriptor.get("sensitivity", SENSITIVITY_NONE)
    confidence = float(match.get("confidence", 0.0) or 0.0)

    # Decision 1 — pre-ladder gate. Sensitivity ≥ financial → refuse, period.
    sens_rank = (_SENSITIVITY_ORDER.index(sensitivity)
                 if sensitivity in _SENSITIVITY_ORDER
                 else _SENSITIVITY_ORDER.index(SENSITIVITY_NONE))
    if sens_rank >= _SENSITIVITY_ORDER.index(SENSITIVITY_FINANCIAL):
        decision = {
            "branch": BRANCH_REFUSE_SENSITIVE,
            "reason": f"sensitivity '{sensitivity}' above personal — human keypress only",
        }
    elif field_descriptor.get("current_value") and not field_descriptor.get("operator_override_clobber"):
        # Field is pre-filled — default to "leave alone, ask to confirm/overwrite."
        decision = {
            "branch": BRANCH_FILL_WITH_CONFIRM,
            "reason": "current_value present; default leave-and-confirm",
            "suggested_value": match.get("value"),
        }
    elif confidence >= AUTO_FILL_CONFIDENCE_THRESHOLD:
        decision = {
            "branch": BRANCH_AUTO_FILL_FLAG,
            "reason": f"confidence {confidence:.2f} ≥ auto-fill threshold",
            "suggested_value": match.get("value"),
        }
    elif confidence >= FILL_WITH_CONFIRM_THRESHOLD:
        decision = {
            "branch": BRANCH_FILL_WITH_CONFIRM,
            "reason": f"confidence {confidence:.2f} between auto-fill and confirm thresholds",
            "suggested_value": match.get("value"),
        }
    elif confidence >= DISAMBIGUATE_THRESHOLD:
        decision = {
            "branch": BRANCH_DISAMBIGUATE,
            "reason": f"confidence {confidence:.2f} below confirm threshold; show candidates",
            "candidates": match.get("candidates", []),
        }
    else:
        decision = {
            "branch": BRANCH_STOP_AND_ASK,
            "reason": f"no confident match (confidence={confidence:.2f}); stop for keypress",
        }

    # Decision 2 (and decision 1) both emit one audit entry. Every branch.
    suggested = decision.get("suggested_value")
    _audit_log({
        "field_ref": field_descriptor.get("ref"),
        "field_label_visible": (field_descriptor.get("label") or {}).get("visible"),
        "field_type": field_descriptor.get("field_type"),
        "sensitivity": sensitivity,
        "branch": decision["branch"],
        "match_confidence": confidence,
        "match_signal_source": match.get("source"),
        "profile_field_used": match.get("profile_field"),
        "value_recorded": _audit_value(suggested, sensitivity,
                                       field_descriptor.get("field_type")),
        "value_redacted_reason": _audit_redact_reason(sensitivity,
                                                     field_descriptor.get("field_type"),
                                                     has_value=(suggested is not None)),
    }, path=audit_path)

    return decision


def _build_interrupt_payload(decision: dict, field_descriptor: dict, step_id: str = "") -> dict:
    """Build the operator-facing payload for a non-auto_fill branch. Includes
    an opaque resume_token whose structured record only the audit DB sees."""
    import time, secrets, hashlib
    token_id = secrets.token_hex(8)
    now_ms = int(time.time() * 1000)
    sensitivity = field_descriptor.get("sensitivity", SENSITIVITY_NONE)
    ttl = (INTERRUPT_TTL_MS_SENSITIVE
           if sensitivity != SENSITIVITY_NONE
           else INTERRUPT_TTL_MS_NORMAL)

    sig_input = (field_descriptor.get("ref", "")
                 + str(field_descriptor.get("label", {})))
    field_signature = hashlib.sha256(sig_input.encode()).hexdigest()[:16]

    return {
        "branch": decision["branch"],
        "field_descriptor": field_descriptor,
        "why": decision.get("reason", ""),
        "suggested_value": decision.get("suggested_value"),
        "candidates": decision.get("candidates"),
        "resume_token": {
            "token_id": token_id,
            "ts_emitted": now_ms,
            "ts_expires": now_ms + ttl,
            "field_signature_at_emit": field_signature,
            "step_id_at_emit": step_id,
        },
    }


# ─── page_signals — cycle 1: producer's core ─────────────────────────
#
# read_form's output → fill_form's input. The producer decides "this step
# is hydrated and ready to fill" vs "wait, still loading" vs "we hit a
# validation error after the last fill." Designed in dual-agent dialogue
# with browser-Claude 2026-05-18.
#
# Cycle 1 scope (this commit): dataclasses + pure producer function +
# shape-only smoke test. Stub adapter from raw page text — partial,
# many slots default to None / False / empty. Future cycles upgrade
# the adapter and add the consumer + test matrix.
#
# Schema decision rationale: 11 slots over my initial 7. Schema shape
# is cheap to fix now and expensive to retrofit across cycles 2 and 3.

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PageContext:
    """Structured representation of the most recent BROWSER_READ_PAGE
    return. Cycle 1: mostly populated from the raw text by the stub
    adapter `_pagecontext_from_directive_results`. Cycle 2+ adds richer
    DOM-derived fields (input counts, class attributes, aria-busy).
    Cycle 3: dom_forms carries the structured form/field list the
    extension's domState() emits, parsed from the JSON BROWSER_READ_PAGE
    payload. Each entry shape matches sensei_extension's content_script
    domState() output:
        {index, selector, fields: [{role, name, selector, type, value_present}]}

    Future upgrade path: incorporate network-idle signal from the
    extension's network observation surface when available."""
    raw: str = ""
    url: Optional[str] = None
    title: Optional[str] = None
    has_form_content: bool = False  # heuristic: form-shaped page detected
    dom_forms: List[dict] = field(default_factory=list)  # cycle 3: structured form/field tree


@dataclass
class PageSignals:
    """Producer output consumed by fill_form. Each slot answers a
    distinct question for the consumer; collapsing them loses info.
    Most-False/None on an empty PageContext is the correct degraded
    state — fill_form should treat that as 'wait, still loading.'

    Eight slots. The dropped trio (step_progress_source / page_url /
    page_title) were degenerate in cycle 1 — nothing in fill_form would
    couple to them, and Optional[X] = None fields can be added in a
    later cycle without breaking callers. Browser-Claude conceded the
    refactor 2026-05-18 — schema-shape stability matters when consumers
    couple to the shape, and nothing couples to these three."""
    step_index: Optional[int] = None         # this step is N of total
    total_steps: Optional[int] = None
    is_submit_step: bool = False             # last step before final submit
    is_hydrated: bool = False                # form is stable, safe to read
    has_blocking_errors: bool = False        # validation failed somewhere
    validation_errors: List[dict] = field(default_factory=list)
    continue_button_present: bool = False
    continue_button_enabled: bool = False


@dataclass
class FormDescriptorRecord:
    """The structured slot value at state.data[_STATE_KEY_FORM_DESCRIPTORS].

    Carries everything fill_form needs for the freshness check: step_id so
    fill_form can validate the descriptors match the current step,
    ts_read so fill_form can detect stale reads, page_signals snapshot at
    read time so fill_form can compare against fresh signals, and the
    descriptors list itself.

    step_id derivation (cycle 2): f"step_{page_signals.step_index}" when
    step_index is non-None, else "step_0". Full derivation (URL hash vs
    heading hash vs hybrid) stays deferred per browser-Claude design
    review 2026-05-18 — pick blind costs nothing to defer."""
    step_id: str
    ts_read: str          # ISO 8601 UTC microsecond
    page_signals: PageSignals
    descriptors: list     # List[dict] of FieldDescriptor-shaped entries


_STEP_PROGRESS_PATTERNS = [
    # Common copy: "Step 2 of 5", "Question 3 / 8", "Page 1 of 4"
    re.compile(r"\b(?:step|question|page)\s+(\d+)\s*(?:of|/)\s*(\d+)\b", re.IGNORECASE),
]

_SUBMIT_BUTTON_PATTERNS = [
    # Word-boundary match so the pattern fires when the verb appears in
    # button copy mid-text. Documented as best-effort: may false-positive
    # on confirmation pages or non-button text containing these verbs.
    re.compile(r"\bsubmit\b", re.IGNORECASE),
    re.compile(r"\bsend\b", re.IGNORECASE),
    re.compile(r"\bfinish\b", re.IGNORECASE),
    # "apply" is in <button>Apply</button> AND in form copy like "Apply
    # the changes" — accepted false-positive risk.
    re.compile(r"\bapply\b", re.IGNORECASE),
]

_CONTINUE_BUTTON_PATTERNS = [
    re.compile(r"\bcontinue\b", re.IGNORECASE),
    re.compile(r"\bnext\b", re.IGNORECASE),
]

_LOADING_PHRASES = [
    "loading...",
    "please wait",
    "one moment",
    "submitting...",
]


def _pagecontext_from_directive_results(raw: str) -> PageContext:
    """Adapter from BROWSER_READ_PAGE raw return to PageContext.

    Cycle 3: tries JSON parsing first to extract the structured page_context
    fields (url, title, dom_state.forms). Falls back to the text-only
    cycle-1 heuristic if the input isn't JSON-shaped, so callers passing
    legacy raw strings still work.
    """
    if not raw:
        return PageContext(raw="")

    # Try structured JSON first (the modern path: extension returns
    # {ok, page_context: {...}, text} — caller may have passed the whole
    # response or just the page_context block).
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        obj = None

    if isinstance(obj, dict):
        ctx_obj = obj.get("page_context") if "page_context" in obj else obj
        if isinstance(ctx_obj, dict):
            url = ctx_obj.get("url")
            title = ctx_obj.get("title")
            dom_state = ctx_obj.get("dom_state") or {}
            forms = dom_state.get("forms") if isinstance(dom_state, dict) else None
            forms = forms if isinstance(forms, list) else []
            visible_text = ctx_obj.get("visible_text") or ""
            has_form = bool(forms) or bool(
                dom_state.get("counts", {}).get("inputs") if isinstance(dom_state, dict) else 0
            )
            return PageContext(
                raw=visible_text or raw,
                url=str(url) if url else None,
                title=str(title) if title else None,
                has_form_content=has_form,
                dom_forms=forms,
            )

    # Legacy text-only fallback (cycle 1 path) — keeps old callers working.
    has_form = "<form" in raw.lower() or "input" in raw.lower()
    return PageContext(raw=raw, has_form_content=has_form)


# ─── Cycle 3: label → semantic field_role mapping ─────────────────────
# Each entry: (compiled regex matching the label_visible, canonical
# field_role from FIELD_ROLE_TO_SENSITIVITY in the active ATS adapter).
# First match wins. Order matters — more specific patterns first.
# Adding a new field_role to the adapter? Add a matcher here too.

_LABEL_TO_FIELD_ROLE = [
    # name fields — order matters: "first" before generic "name"
    (re.compile(r"\b(first[\s_-]*name|given[\s_-]*name|forename)\b", re.IGNORECASE), "name_first"),
    (re.compile(r"\b(last[\s_-]*name|surname|family[\s_-]*name)\b", re.IGNORECASE), "name_last"),
    (re.compile(r"\b(preferred[\s_-]*name|nickname|goes[\s_-]*by)\b", re.IGNORECASE), "name_preferred"),
    (re.compile(r"\b(full[\s_-]*name|legal[\s_-]*name|name\s*$)\b", re.IGNORECASE), "name_full"),
    # contact
    (re.compile(r"\b(e[\s_-]*mail|email[\s_-]*address)\b", re.IGNORECASE), "email"),
    (re.compile(r"\b(phone|mobile|telephone|cell)\b", re.IGNORECASE), "phone"),
    # address
    (re.compile(r"\b(street|address[\s_-]*line[\s_-]*1|address(?!.*2))\b", re.IGNORECASE), "address_line_1"),
    (re.compile(r"\baddress[\s_-]*line[\s_-]*2|apt|suite|unit\b", re.IGNORECASE), "address_line_2"),
    (re.compile(r"\bcity\b|\btown\b", re.IGNORECASE), "city"),
    (re.compile(r"\b(state|province|region)\b", re.IGNORECASE), "state"),
    (re.compile(r"\b(zip|postal[\s_-]*code|postcode)\b", re.IGNORECASE), "zip"),
    (re.compile(r"\bcountry\b", re.IGNORECASE), "country"),
    (re.compile(r"\b(date[\s_-]*of[\s_-]*birth|dob|birthday)\b", re.IGNORECASE), "date_of_birth"),
    # work-history adjacent
    (re.compile(r"\b(years?[\s_-]*of[\s_-]*experience|experience[\s_-]*years?)\b", re.IGNORECASE), "experience_years"),
    (re.compile(r"\b(education|degree|highest[\s_-]*level)\b", re.IGNORECASE), "education_level"),
    (re.compile(r"\b(work[\s_-]*history|employment[\s_-]*history)\b", re.IGNORECASE), "work_history"),
    (re.compile(r"\bcover[\s_-]*letter\b", re.IGNORECASE), "cover_letter"),
    (re.compile(r"\b(linkedin|linked[\s_-]*in)\b", re.IGNORECASE), "linkedin_url"),
    (re.compile(r"\b(resume|cv)\s*(upload|attach|file)?\b", re.IGNORECASE), "resume_upload"),
    # financial
    (re.compile(r"\b(salary|compensation|expected[\s_-]*pay)\b", re.IGNORECASE), "salary_expectation"),
    (re.compile(r"\b(routing[\s_-]*number|bank[\s_-]*routing)\b", re.IGNORECASE), "bank_routing"),
    (re.compile(r"\b(account[\s_-]*number|bank[\s_-]*account)\b", re.IGNORECASE), "bank_account"),
    # government-id (refuse-sensitive — never auto-fill regardless of confidence)
    (re.compile(r"\b(ssn|social[\s_-]*security)\b", re.IGNORECASE), "ssn"),
    (re.compile(r"\bein\b|\bemployer[\s_-]*identification\b", re.IGNORECASE), "ein"),
    (re.compile(r"\bpassport\b", re.IGNORECASE), "passport_number"),
    (re.compile(r"\b(driver'?s?[\s_-]*license|dl\s*number)\b", re.IGNORECASE), "drivers_license"),
    (re.compile(r"\bimmigration\b|\bvisa[\s_-]*number\b", re.IGNORECASE), "immigration_number"),
    (re.compile(r"\b(work[\s_-]*auth(orization)?|i-9|eligibility[\s_-]*to[\s_-]*work)\b", re.IGNORECASE), "work_authorization_id"),
]


def _label_to_field_role(label: str) -> Optional[str]:
    """Map a visible field label to a canonical field_role.

    Returns the first matching role, or None if no pattern fires.
    Caller can treat None as 'unknown / surface to operator' — the
    executor's stop_and_ask branch handles that case.
    """
    if not label:
        return None
    for pattern, role in _LABEL_TO_FIELD_ROLE:
        if pattern.search(label):
            return role
    return None


def _extract_form_descriptors(ctx: PageContext, step_index: Optional[int]) -> list:
    """Walk PageContext.dom_forms and produce descriptor dicts in the
    shape FormDescriptorRecord.descriptors carries (and that
    _executor_decide consumes).

    Output shape per entry (matches the synthetic descriptor format
    in cycle-2 stub + adds 'sensitivity' resolved at extraction time):
        {field_role, label_visible, css_selector, html_input_type,
         required, step_index}

    field_role is mapped from the field's `name` (rendered label) via
    _label_to_field_role. Fields with no matching role get
    field_role=None — _executor_decide's stop_and_ask branch handles
    that downstream; we don't drop them silently here.

    Required-ness: the extension's dom_state.forms[].fields[] doesn't
    currently surface a `required` flag. Cycle 3.1 heuristic: an
    asterisk in the label, or html input attribute when surfaced in
    future cycles. Until then required defaults False — safer (won't
    error-block on optional fields) than True.
    """
    descriptors = []
    for form in (ctx.dom_forms or []):
        if not isinstance(form, dict):
            continue
        for fld in (form.get("fields") or []):
            if not isinstance(fld, dict):
                continue
            label = str(fld.get("name") or "").strip()
            selector = str(fld.get("selector") or "").strip()
            if not selector:
                # Without a selector the field is not fillable; skip.
                continue
            field_role = _label_to_field_role(label)
            html_type = str(fld.get("type") or "").strip()
            # Required heuristic: asterisk in label is the most reliable
            # cross-ATS signal; refine in cycle 3.1 with aria-required.
            required = "*" in label
            descriptors.append({
                "field_role": field_role,
                "label_visible": label,
                "css_selector": selector,
                "html_input_type": html_type,
                "required": required,
                "step_index": step_index,
                "_role_match": field_role is not None,
            })
    return descriptors


def page_signals_from_context(
    ctx: PageContext,
    previous_context: Optional[PageContext] = None,
) -> PageSignals:
    """Pure function: PageContext → PageSignals. No state, no side
    effects. Caller decides whether to do two reads and pass the prior
    context (stability check); function falls back to single-read
    heuristics when previous_context is None.

    Cycle 1: heuristics work mostly on raw page text. Cycle 2 fills in
    URL/title/structured validation errors. Cycle 3 lands the consumer
    wiring in fill_form + the full test matrix."""
    if not ctx or not ctx.raw:
        return PageSignals()  # all defaults — degraded "wait" state

    raw = ctx.raw
    raw_lower = raw.lower()

    signals = PageSignals()

    # step_index / total_steps from "Step N of M" text patterns
    for pat in _STEP_PROGRESS_PATTERNS:
        m = pat.search(raw)
        if m:
            try:
                signals.step_index = int(m.group(1))
                signals.total_steps = int(m.group(2))
                break
            except (ValueError, IndexError):
                continue

    # continue_button_present: look for forward-action button text
    signals.continue_button_present = any(
        p.search(raw) for p in _CONTINUE_BUTTON_PATTERNS
    )
    # enabled — without DOM state we can't tell disabled vs enabled;
    # default = present. Cycle 2 refines.
    signals.continue_button_enabled = signals.continue_button_present

    # is_submit_step: submit-flavored button present AND no continue/next.
    # Per browser-Claude design: button-text heuristic, may false-positive
    # on confirmation pages — documented as best-effort.
    has_submit_button = any(p.search(raw) for p in _SUBMIT_BUTTON_PATTERNS)
    signals.is_submit_step = has_submit_button and not signals.continue_button_present

    # If step_index + total_steps known, prefer that signal for is_submit_step
    if signals.step_index is not None and signals.total_steps is not None:
        signals.is_submit_step = signals.step_index >= signals.total_steps

    # is_hydrated: cycle-1 single-read heuristic. True if context has
    # form content AND no loading phrases visible. Caller can do a
    # two-read stability check by passing previous_context.
    has_loading_phrase = any(p in raw_lower for p in _LOADING_PHRASES)
    base_hydrated = ctx.has_form_content and not has_loading_phrase

    if previous_context is not None and previous_context.raw:
        # Stability check: raw length stable within 2% AND has_form_content
        # matches AND prev had form content too.
        prev_len = len(previous_context.raw)
        curr_len = len(raw)
        if prev_len > 0:
            delta_ratio = abs(curr_len - prev_len) / prev_len
            stable = (delta_ratio < 0.02
                      and previous_context.has_form_content == ctx.has_form_content
                      and ctx.has_form_content)
            signals.is_hydrated = stable and not has_loading_phrase
        else:
            signals.is_hydrated = base_hydrated
    else:
        signals.is_hydrated = base_hydrated

    # has_blocking_errors / validation_errors — cycle-1 heuristic only
    # detects presence via common error markers in text. Structured
    # error extraction is a cycle 2 expansion.
    error_markers = ["please fix", "required field", "invalid", "error:"]
    if any(m in raw_lower for m in error_markers):
        signals.has_blocking_errors = True
        # Validation_errors stays empty list in cycle 1 — text extraction
        # without structured DOM is too unreliable to populate.

    return signals


# ─── read_form_current_step / fill_form_current_step phases ──────────
#
# Step-scoped phase functions per browser-Claude design review 2026-05-18.
# read_form_current_step owns the page read, retry logic, and descriptor
# extraction; fill_form_current_step (cycle-3 work) does the gate-check +
# match-then-dispatch; cycle 2 ships read_form full + fill_form stub.
#
# state.data slot contract (locked in same design review):
#   _form_descriptors_current_step  — the FormDescriptorRecord
#   _read_form_retry_count          — int, hydration-retry counter
#   _read_form_previous_context     — PageContext for stability checks
#   _initial_read_dispatched        — bool, true after first BROWSER_READ_PAGE
#                                     emitted on this step (added per BC's
#                                     refinement to keep the first-call/no-
#                                     read-yet branch from burning a retry slot)

_STATE_KEY_FORM_DESCRIPTORS = "_form_descriptors_current_step"
_STATE_KEY_READ_RETRY = "_read_form_retry_count"
_STATE_KEY_PREV_CONTEXT = "_read_form_previous_context"
_STATE_KEY_INITIAL_READ_DISPATCHED = "_initial_read_dispatched"

_READ_FORM_MAX_RETRIES = 3


def _read_form_step_id(page_signals: PageSignals) -> str:
    """Cycle-2 step_id derivation: f"step_{step_index}" when non-None,
    "step_0" fallback. Full derivation (URL hash / heading hash / hybrid)
    stays deferred — committing to a derivation now without knowing what
    fill_form (cycle 3) actually needs is picking blind."""
    if page_signals.step_index is not None:
        return f"step_{page_signals.step_index}"
    return "step_0"


def redirect_check_at_phase_entry(
    task,
    current_tab_urls: dict,
    host_adapters: dict = None,
) -> dict:
    """Adapter-wiring v0 entry hook — adapters call this BEFORE any DOM
    read, per browser-Claude design 2026-05-18.

    Compares the task's current primary binding (last_observed_url) against
    the live tab URL. Returns a decision the adapter applies via the phase
    outcome's _side_effects channel:

      {"action": "proceed"}
          Primary binding's tab is still on the bound URL. Caller can
          read the DOM with the expected selectors.

      {"action": "observe", "_side_effects": [...]}
          Primary tab navigated within the SAME host (e.g., Workday
          step1 → step2). Refresh last_observed_url but DON'T burn a
          redirect_chain slot or fire promote. Caller proceeds to DOM
          read after applying the side effect.

      {"action": "promote", "_side_effects": [...]}
          Primary tab navigated to a DIFFERENT but KNOWN host (e.g.,
          indeed.com → smartapply.indeed.com, or → myworkdayjobs.com).
          Returns demote-old-primary + add-new-primary side effects
          (via detect_redirect_and_promote). Caller MUST NOT proceed
          with the current step on the old binding — re-enter phase
          dispatcher.

      {"action": "unsupported_host", "host": "..."}
          Primary tab navigated to a host NOT in host_adapters. Caller
          returns an interrupt with details.reason='unsupported_host'
          so the dispatcher can set attention to operator. Per BC's
          A-refinement — no adapter_unknown registration system in v0;
          we use the existing INTERRUPTED state with a structured
          reason code.

      {"action": "no_primary"}
          Task has no primary binding yet. Adapter should not be
          running at this point; caller surfaces as interrupt for
          operator to designate the primary tab.

    Why pure: caller is responsible for applying the side effects via the
    phase outcome → dispatcher path. Keeps redirect-checking testable in
    isolation and lets the same logic work for read_form_current_step,
    fill_form_current_step, submit_gate, and verify phases."""
    from task_model import (
        TaskTabBinding,
        BINDING_ROLE_PRIMARY,
        BINDING_SOURCE_ADAPTER_PROMOTED,
        SIDE_EFFECT_TAB_BINDING_OBSERVE,
        detect_redirect_and_promote,
    )

    if host_adapters is None:
        host_adapters = HOST_ADAPTERS

    def _find_adapter(host: str):
        for suffix, fn_name in host_adapters.items():
            if host.endswith(suffix) or suffix in host:
                return fn_name
        return None

    primary = None
    for b in task.tab_bindings:
        if b.role == BINDING_ROLE_PRIMARY:
            primary = b
            break

    if primary is None:
        return {"action": "no_primary"}

    primary_current_url = current_tab_urls.get(primary.tab_id)
    if primary_current_url is None:
        # Tab closed — route_for_task will surface this as STALE_PRIMARY
        # next dispatch. Adapter shouldn't try to recover here.
        return {
            "action": "stale_primary",
            "reason": "primary_tab_closed",
            "tab_id": primary.tab_id,
        }

    bound_host = (urlparse(primary.last_observed_url or "").hostname or "").lower()
    primary_current_host = (urlparse(primary_current_url).hostname or "").lower()

    # Case 2 check first: did a NEW tab open on a known ATS host? This
    # covers the click-Apply pattern where Indeed opens smartapply in a
    # new tab while the listing tab stays put. Has priority over
    # primary-drift because if a new tab opened, that's where we should
    # be acting now, regardless of whether the listing tab also moved.
    bound_tab_ids = {b.tab_id for b in task.tab_bindings}
    new_tab_promotes = []
    for tab_id in sorted(set(current_tab_urls) - bound_tab_ids):
        url = current_tab_urls[tab_id]
        host = (urlparse(url).hostname or "").lower()
        adapter = _find_adapter(host)
        if adapter is not None:
            new_tab_promotes.append((tab_id, host, adapter))

    if new_tab_promotes:
        tab_id, host, adapter = new_tab_promotes[0]
        previous_urls = {primary.tab_id: primary.last_observed_url or ""}
        side_effects = detect_redirect_and_promote(
            task=task,
            previous_tab_urls=previous_urls,
            current_tab_urls=current_tab_urls,
            redirect_host_substring=host,
        )
        return {
            "action": "promote",
            "to_host": host,
            "to_adapter": adapter,
            "_side_effects": side_effects,
        }

    # Case 1: primary tab drift in-place.
    if not bound_host or primary_current_url == primary.last_observed_url:
        # No drift — either we never recorded a URL (first read), or the
        # tab is still on the same URL we read last time.
        return {"action": "proceed"}

    if bound_host == primary_current_host:
        # Same-host page transition. Refresh last_observed_url via
        # observe side-effect; don't append to redirect_chain. Per BC's
        # B-with-host-change-append refinement — multi-step Workday forms
        # walk through several URLs on the same host and shouldn't burn
        # the cap.
        return {
            "action": "observe",
            "_side_effects": [{
                "kind": SIDE_EFFECT_TAB_BINDING_OBSERVE,
                "tab_id": primary.tab_id,
                "new_last_observed_url": primary_current_url,
            }],
        }

    # Cross-host drift in primary tab. Is the new host known?
    adapter_name = _find_adapter(primary_current_host)
    if adapter_name is None:
        return {
            "action": "unsupported_host",
            "host": primary_current_host,
            "tab_id": primary.tab_id,
            "from_host": bound_host,
        }

    # Known host, in-tab redirect — build the promote side effects.
    previous_urls = {primary.tab_id: primary.last_observed_url or ""}
    side_effects = detect_redirect_and_promote(
        task=task,
        previous_tab_urls=previous_urls,
        current_tab_urls=current_tab_urls,
        redirect_host_substring=primary_current_host,
    )
    return {
        "action": "promote",
        "to_host": primary_current_host,
        "to_adapter": adapter_name,
        "_side_effects": side_effects,
    }


def read_form_current_step(state) -> dict:
    """Phase function: read the current step's form, build FormDescriptorRecord,
    write to state.data. Three branches at entry per browser-Claude refinement:

      1. _initial_read_dispatched is False — emit BROWSER_READ_PAGE, interrupt
         with `awaiting_initial_read`. Re-enters next round to consume result.
         Keeps the first-call/no-read-yet case from burning a hydration retry.

      2. _initial_read_dispatched is True but no _last_directive_results —
         directive execution failure (the read didn't fire). Interrupt with
         `read_directive_failed`. NOT a retry-slot burn; that's reserved for
         hydration failures specifically.

      3. _last_directive_results present — run page_signals_from_context.
         is_hydrated True → write FormDescriptorRecord, transition to
         fill_form_current_step. is_hydrated False → increment retry count,
         emit BROWSER_WAIT + BROWSER_READ_PAGE if budget remains, else
         interrupt with `hydration_failed_after_3_attempts`.

    Returns {outcome, details} with the same shape as adapter_* phase
    functions. Side-effect-only on state.data (writes to the slots above)."""
    import datetime

    initial_read_dispatched = state.data.get(_STATE_KEY_INITIAL_READ_DISPATCHED, False)
    last_results = state.data.get("_last_directive_results")

    # Branch 1: first invocation on this step — dispatch the read.
    if not initial_read_dispatched:
        return {
            "outcome": "interrupt",
            "details": {
                "_pending_directives": ["BROWSER_READ_PAGE: form"],
                "reason": "awaiting_initial_read",
                "_state_update": {_STATE_KEY_INITIAL_READ_DISPATCHED: True},
            },
        }

    # Branch 2: read dispatched but no result — directive failure.
    if not last_results:
        return {
            "outcome": "interrupt",
            "details": {
                "reason": "read_directive_failed",
            },
        }

    # Branch 3: read result present — build context and check hydration.
    ctx = _pagecontext_from_directive_results(last_results)
    prev_ctx = state.data.get(_STATE_KEY_PREV_CONTEXT)
    signals = page_signals_from_context(ctx, previous_context=prev_ctx)

    if signals.is_hydrated:
        # Build the record and transition to fill_form_current_step.
        ts_iso = (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="microseconds")
        )
        # Cycle 3: real DOM extraction from PageContext.dom_forms. The
        # synthetic-descriptor stub is gone; we walk the extension's
        # domState() output and produce one descriptor per fillable field.
        descriptors = _extract_form_descriptors(ctx, signals.step_index)
        record = FormDescriptorRecord(
            step_id=_read_form_step_id(signals),
            ts_read=ts_iso,
            page_signals=signals,
            descriptors=descriptors,
        )
        return {
            "outcome": "interrupt",
            "details": {
                "reason": "form_read_complete_transition_to_fill",
                "_state_update": {
                    _STATE_KEY_FORM_DESCRIPTORS: record,
                    _STATE_KEY_READ_RETRY: 0,  # reset on success
                },
            },
        }

    # Not hydrated — retry budget logic.
    retry_count = int(state.data.get(_STATE_KEY_READ_RETRY, 0))
    if retry_count >= _READ_FORM_MAX_RETRIES:
        return {
            "outcome": "interrupt",
            "details": {
                "reason": f"hydration_failed_after_{_READ_FORM_MAX_RETRIES}_attempts",
            },
        }

    return {
        "outcome": "interrupt",
        "details": {
            "_pending_directives": [
                "BROWSER_WAIT: 500",
                "BROWSER_READ_PAGE: form",
            ],
            "reason": "hydration_retry",
            "_state_update": {
                _STATE_KEY_READ_RETRY: retry_count + 1,
                _STATE_KEY_PREV_CONTEXT: ctx,
            },
        },
    }


# ─── Cycle 3: fill_form match-loop helpers ────────────────────────────

# Static across ATSs (per design review 2026-05-18): sensitivity rules
# live in ONE place. Mirrored from atss/indeed_smart_apply.py until
# we move that constant out of the per-ATS file into recipe.py proper.
_FIELD_ROLE_TO_SENSITIVITY = {
    # none — public/innocuous
    "name_first": "none", "name_last": "none", "name_preferred": "none",
    "name_full": "none", "experience_years": "none", "education_level": "none",
    "work_history": "none", "cover_letter": "none", "linkedin_url": "none",
    "resume_upload": "none",
    # personal — fingerprint-redacted in audit log
    "email": "personal", "phone": "personal",
    "address_line_1": "personal", "address_line_2": "personal",
    "city": "personal", "state": "personal", "zip": "personal",
    "country": "personal", "date_of_birth": "personal",
    # financial — refuse-sensitive, human keypress only
    "salary_expectation": "financial",
    "bank_routing": "financial", "bank_account": "financial",
    # government_id — refuse-sensitive, human keypress only
    "ssn": "government_id", "ein": "government_id",
    "passport_number": "government_id", "drivers_license": "government_id",
    "immigration_number": "government_id", "work_authorization_id": "government_id",
}

# Map canonical field_role → profile JSON key. Roles missing here have
# no auto-source from the profile and route to stop_and_ask.
_PROFILE_KEY_FOR_ROLE = {
    "name_first": "first_name",
    "name_last": "last_name",
    "name_full": "full_name",
    "name_preferred": "preferred_name",
    "email": "email",
    "phone": "phone",
    "address_line_1": "address",
    "address_line_2": "address_line_2",
    "city": "city",
    "state": "state",
    "zip": "zip",
    "country": "country",
    "linkedin_url": "linkedin_url",
    "experience_years": "experience_years",
    "education_level": "education_level",
    "work_history": "work_history",
}


def _load_profile_or_none() -> Optional[dict]:
    """Load the operator's profile from disk. Returns None if missing
    or invalid — caller treats absent profile as 'cannot match' and
    routes all fields to stop_and_ask."""
    try:
        return json.loads(PROFILE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _match_profile_to_descriptor(profile: Optional[dict], descriptor: dict) -> dict:
    """Build the `match` dict _executor_decide expects from a profile
    and a descriptor produced by _extract_form_descriptors.

    Returns {confidence, value, source, profile_field, candidates}.
    """
    field_role = descriptor.get("field_role")
    if not field_role or not profile:
        return {
            "confidence": 0.0,
            "value": None,
            "source": "no_profile" if not profile else "unknown_role",
            "profile_field": None,
            "candidates": [],
        }
    profile_key = _PROFILE_KEY_FOR_ROLE.get(field_role)
    if not profile_key:
        return {
            "confidence": 0.0,
            "value": None,
            "source": f"no_profile_mapping_for_{field_role}",
            "profile_field": None,
            "candidates": [],
        }
    value = profile.get(profile_key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return {
            "confidence": 0.0,
            "value": None,
            "source": f"profile_field_{profile_key}_empty",
            "profile_field": profile_key,
            "candidates": [],
        }
    # Direct role match + populated profile field = high confidence.
    return {
        "confidence": 0.95,
        "value": value,
        "source": "role_to_profile_field",
        "profile_field": profile_key,
        "candidates": [],
    }


def _descriptor_to_executor_input(descriptor: dict) -> dict:
    """Adapt an _extract_form_descriptors output to the shape
    _executor_decide expects.

    Maps:
      css_selector → ref
      label_visible → label.visible
      html_input_type → field_type
      field_role → sensitivity (via _FIELD_ROLE_TO_SENSITIVITY)
    """
    role = descriptor.get("field_role")
    sensitivity = _FIELD_ROLE_TO_SENSITIVITY.get(role, "none") if role else "none"
    return {
        "ref": descriptor.get("css_selector"),
        "label": {"visible": descriptor.get("label_visible"), "aria": None, "placeholder": None, "legend": None},
        "name": descriptor.get("css_selector"),
        "id": None,
        "field_type": descriptor.get("html_input_type"),
        "required": descriptor.get("required", False),
        "options": None,
        "current_value": "",  # cycle 3.1 will surface value_present from dom_state
        "editability": "editable",
        "sensitivity": sensitivity,
    }


def fill_form_current_step(state) -> dict:
    """Cycle 3 real-mode: gate-then-match flow.

    1. Presence check — descriptors must exist (read_form built them).
    2. Errors gate — page_signals.has_blocking_errors → interrupt for
       operator to resolve validation errors before continuing.
    3. Submit-step gate — is_submit_step routes to submit_gate (separate
       phase, irreversible action handling).
    4. For each descriptor: build match from profile, run _executor_decide,
       collect BROWSER_FILL directives for auto_fill_flag, route other
       branches to operator (stop_and_ask, disambiguate, refuse_sensitive)
       via an interrupt with the field's metadata.
    5. Return either:
       - {outcome: interrupt, _pending_directives: [BROWSER_FILL ...]}
         when one or more fields auto-fill (operator sees the fills land)
       - {outcome: interrupt, reason: <handoff_reason>, field: <descriptor>}
         on the first non-auto-fill branch (operator decides per field)
    """
    record = state.data.get(_STATE_KEY_FORM_DESCRIPTORS)
    if record is None:
        return {
            "outcome": "interrupt",
            "details": {"reason": "descriptors_missing"},
        }

    signals = record.page_signals

    # Gate 1: validation errors block forward motion until operator resolves.
    if signals.has_blocking_errors:
        return {
            "outcome": "interrupt",
            "details": {
                "reason": "validation_errors_present",
                "validation_errors": signals.validation_errors,
            },
        }

    # Gate 2: submit step routes to submit_gate_phase (irreversible).
    if signals.is_submit_step:
        return {
            "outcome": "interrupt",
            "details": {
                "reason": "submit_step_detected_route_to_submit_gate",
                "step_index": signals.step_index,
            },
        }

    profile = _load_profile_or_none()
    fill_directives: List[str] = []
    handoff = None

    for descriptor in record.descriptors:
        match = _match_profile_to_descriptor(profile, descriptor)
        exec_input = _descriptor_to_executor_input(descriptor)
        decision = _executor_decide(exec_input, match)
        branch = decision.get("branch")
        if branch == BRANCH_AUTO_FILL_FLAG:
            selector = descriptor.get("css_selector")
            value = decision.get("suggested_value")
            if selector and value is not None:
                # BROWSER_FILL syntax per master_ai.py: <selector> :: <value>
                fill_directives.append(f"BROWSER_FILL: {selector} :: {value}")
        elif handoff is None:
            # First non-auto-fill branch becomes the handoff to operator.
            # Subsequent fields wait until this one resolves and fill_form
            # re-runs after the operator's keypress / confirmation.
            handoff = {
                "branch": branch,
                "field_role": descriptor.get("field_role"),
                "label_visible": descriptor.get("label_visible"),
                "css_selector": descriptor.get("css_selector"),
                "decision_reason": decision.get("reason"),
            }

    if fill_directives:
        return {
            "outcome": "interrupt",
            "details": {
                "reason": "fields_filled_pending_resume",
                "_pending_directives": fill_directives,
                "handoff_pending": handoff,  # next non-fillable field, if any
            },
        }

    if handoff:
        return {
            "outcome": "interrupt",
            "details": {
                "reason": f"field_handoff_{handoff['branch']}",
                "handoff": handoff,
            },
        }

    # No fillable fields, no handoffs needed — every descriptor either
    # had no profile match AND was insensitive (unusual but possible).
    return {
        "outcome": "interrupt",
        "details": {
            "reason": "no_actionable_fields",
            "descriptor_count": len(record.descriptors),
        },
    }


# ─── Precondition hook (called by skill_runtime.check_preconditions) ─

def CHECK_PRECONDITIONS() -> None:
    """Hard requirements for skill start. Soft prereqs are checked inside
    load_profile and recorded as warnings."""
    if not PROFILE_PATH.exists():
        raise PreconditionFailed(f"missing profile: {PROFILE_PATH}")
    if not DRIVE_REFS_PATH.exists():
        raise PreconditionFailed(f"missing drive refs: {DRIVE_REFS_PATH}")
    # Permissions check on drive refs (must be 600).
    perms = oct(os.stat(DRIVE_REFS_PATH).st_mode)[-3:]
    if perms != "600":
        raise PreconditionFailed(
            f"drive refs file {DRIVE_REFS_PATH} has perms {perms}, expected 600"
        )
    # Skill runtime importable already (we imported it at module top).
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ─── Step implementations ──────────────────────────────────────────

def _step_load_drive_refs(state: SkillState, params: dict) -> dict:
    """Read the chmod-600 drive refs file. Populate state.data with URLs
    that subsequent steps will navigate to."""
    refs = json.loads(DRIVE_REFS_PATH.read_text())
    ai_query_url = refs.get("ai_query_doc")
    log_url = refs.get("applications_log")
    if not ai_query_url or not log_url:
        return {
            "next": ABORT,
            "state_update": {
                "_aborted_at": "load_drive_refs",
                "_reason": "drive refs file missing ai_query_doc or applications_log key",
            },
        }
    return {
        "next": "load_profile",
        "state_update": {
            "ai_query_url": ai_query_url,
            "applications_log_url": log_url,
            "refs_loaded_at": refs.get("_added_at"),
        },
    }


def _step_load_profile(state: SkillState, params: dict) -> dict:
    """Read the profile file. Validate hard fields; record soft-prereq warnings."""
    profile = json.loads(PROFILE_PATH.read_text())
    required = ["full_name", "email", "phone", "address"]
    missing = [k for k in required if not (profile.get(k) or "").strip()]
    if missing:
        return {
            "next": ABORT,
            "state_update": {
                "_aborted_at": "load_profile",
                "_reason": f"profile missing required: {missing}",
            },
        }
    warnings = []
    if not profile.get("work_history"):
        warnings.append("profile.work_history is empty — ATS forms requiring "
                        "employment history will INTERRUPT for operator input")
    if not (profile.get("recent_job") or "").strip():
        warnings.append("profile.recent_job is blank")
    return {
        "next": "fetch_ai_query_spec",
        "state_update": {
            "profile": {
                # Echo only non-sensitive structural info to state. Full
                # profile lives in the file; steps reload as needed.
                "full_name_len": len(profile.get("full_name", "")),
                "has_email": bool(profile.get("email")),
                "has_phone": bool(profile.get("phone")),
                "work_history_count": len(profile.get("work_history") or []),
                "demographics_keys": sorted((profile.get("demographics") or {}).keys()),
                "screener_defaults_keys": sorted((profile.get("screener_defaults") or {}).keys()),
            },
            "profile_warnings": warnings,
        },
    }


def _step_fetch_ai_query_spec(state: SkillState, params: dict) -> dict:
    """Fetch the AI Query doc via BROWSER_NAV+READ_PAGE, parse rules.
    Skip with empty rules if params['skip_drive_fetches']."""
    if params.get("skip_drive_fetches"):
        return {
            "next": "fetch_applications_log",
            "state_update": {
                "ai_query_rules": {"hard_stops": [], "skip_companies": ["All Trades Staffing"]},
                "_ai_query_skipped": True,
            },
        }
    last = state.data.get("_last_directive_results_by_step", {}).get("fetch_ai_query_spec")
    if last:
        return {
            "next": "fetch_applications_log",
            "state_update": {"ai_query_rules": _parse_ai_query_rules(last)},
        }
    url = state.data.get("ai_query_url")
    return {
        "interrupt": True,
        "interrupt_reason": "fetching AI Query doc",
        "next": "fetch_ai_query_spec",
        "state_update": {
            "_pending_directives": [
                f"BROWSER_NAV: {url}",
                "BROWSER_WAIT: 2000",
                "BROWSER_READ_PAGE: main",
            ],
            "_pending_step": "fetch_ai_query_spec",
        },
    }


def _parse_ai_query_rules(page_text: str) -> dict:
    """Best-effort parse of AI Query doc text. Conservative defaults if
    nothing matches — skill should not fail open."""
    rules = {"hard_stops": [], "skip_companies": []}
    text = page_text or ""
    if re.search(r"all\s*trades\s*staffing", text, re.I):
        rules["skip_companies"].append("All Trades Staffing")
    for m in re.finditer(r"skip[^\n:]*:\s*([^\n]+)", text, re.I):
        rules["hard_stops"].append(m.group(1).strip())
    return rules


def _step_fetch_applications_log(state: SkillState, params: dict) -> dict:
    """Fetch the applications log via BROWSER_NAV+READ_PAGE, parse dedup
    list. Skip with empty dedup if params['skip_drive_fetches']."""
    if params.get("skip_drive_fetches"):
        return {
            "next": "reconcile_inbox",
            "state_update": {
                "applications_log": {"dedup_list": []},
                "_log_skipped": True,
            },
        }
    last = state.data.get("_last_directive_results_by_step", {}).get("fetch_applications_log")
    if last:
        return {
            "next": "reconcile_inbox",
            "state_update": {"applications_log": _parse_applications_log(last)},
        }
    url = state.data.get("applications_log_url")
    return {
        "interrupt": True,
        "interrupt_reason": "fetching applications log",
        "next": "fetch_applications_log",
        "state_update": {
            "_pending_directives": [
                f"BROWSER_NAV: {url}",
                "BROWSER_WAIT: 2000",
                "BROWSER_READ_PAGE: main",
            ],
            "_pending_step": "fetch_applications_log",
        },
    }


def _parse_applications_log(page_text: str) -> dict:
    """Best-effort parse — dedup_list is company names already applied to."""
    dedup = []
    for m in re.finditer(r"(?:applied\s*(?:to)?|submitted\s*to)[:\s]+([A-Z][\w&.,\s-]{2,40})", page_text or "", re.I):
        dedup.append(m.group(1).strip())
    return {"dedup_list": list(dict.fromkeys(dedup))}


def _step_reconcile_inbox(state: SkillState, params: dict) -> dict:
    """Reconcile Gmail+AOL inboxes for confirmation emails. Skip if
    params['skip_inbox'] (IMAP receive not yet wired)."""
    if params.get("skip_inbox"):
        return {
            "next": "enumerate_candidate_jobs",
            "state_update": {"_inbox_skipped": True},
        }
    return {
        "interrupt": True,
        "interrupt_reason": "imap_receive_not_wired_in_v1",
        "next": "reconcile_inbox",
        "state_update": {
            "_pending_step": "reconcile_inbox",
            "_pending_action": "scan Gmail + AOL inboxes for confirmation emails matching every Submitted entry",
        },
    }


def _step_enumerate_candidate_jobs(state: SkillState, params: dict) -> dict:
    """Filter candidate URLs against dedup list + hard stops. If empty
    after filters, INTERRUPT for operator-supplied URLs."""
    candidates = list(params.get("candidate_urls") or [])
    # In v1 the applications_log + ai_query_rules haven't been fetched
    # (those steps INTERRUPT), so dedup is a no-op here. v2 reads
    # state.data["applications_log"]["dedup_list"] and filters.
    dedup_list = (state.data.get("applications_log") or {}).get("dedup_list") or []
    hard_stops = (state.data.get("ai_query_rules") or {}).get("hard_stops") or []
    queue = []
    skipped = {"dedup": [], "hard_stop": [], "residential": []}
    for url in candidates:
        host = (urlparse(url).hostname or "").lower()
        if any(d.lower() in host for d in dedup_list):
            skipped["dedup"].append(url)
            continue
        if any(h.lower() in url.lower() for h in hard_stops):
            skipped["hard_stop"].append(url)
            continue
        # Residential filter heuristic: URL contains "apartment" / "multifamily"
        # / "resident-access" → out. Refined when ai_query_rules is fetched.
        if re.search(r"apartment|multifamily|resident.access", url, re.I):
            skipped["residential"].append(url)
            continue
        queue.append(url)
    if not queue:
        return {
            "interrupt": True,
            "interrupt_reason": "no_candidate_urls_after_filter",
            "next": "enumerate_candidate_jobs",
            "state_update": {
                "queue": [],
                "skipped": skipped,
                "_pending_action": "operator supplies candidate_urls via params or extends params with more URLs and resumes",
            },
        }
    return {
        "next": "apply_one_job",
        "state_update": {
            "queue": queue,
            "skipped": skipped,
            "submitted_count": 0,
        },
    }


def _resolve_adapter_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for suffix, fn_name in HOST_ADAPTERS.items():
        if host.endswith(suffix) or suffix in host:
            return fn_name
    return "adapter_custom"


def _step_apply_one_job(state: SkillState, params: dict) -> dict:
    """Pop one URL from queue (or stick with current_url mid-flow), dispatch
    to per-host adapter, translate adapter result into skill_runtime return.

    The adapter is called once per skill round. Each call returns
    `{outcome: "interrupt"|"applied"|"skipped"|"failed_*", details: {...}}`.
    Interrupts emit BROWSER_* directives and yield to the dispatcher; on
    resume the SAME step fires again and the adapter consumes
    state.data['_last_directive_results'] to advance its phase."""
    queue = list(state.data.get("queue") or [])
    current_url = state.data.get("current_url")
    state_update: dict = {}

    if not current_url:
        if not queue:
            return {"next": "loop_or_done"}
        current_url = queue.pop(0)
        state_update["queue"] = queue
        state_update["current_url"] = current_url
        state_update["_zip_phase"] = None

    adapter_name = _resolve_adapter_name(current_url)
    adapter_fn = globals().get(adapter_name)
    if not adapter_fn:
        state_update["current_url"] = None
        return {
            "next": "loop_or_done",
            "state_update": state_update,
            "artifact_key": "apply_one_job",
            "artifact": {"outcome": "failed_no_adapter", "url": current_url, "adapter": adapter_name},
        }

    profile = json.loads(PROFILE_PATH.read_text())
    rules = state.data.get("ai_query_rules") or {}

    result = adapter_fn(state, current_url, profile, rules)
    outcome = result.get("outcome", "interrupt")
    details = result.get("details") or {}

    adapter_state = details.get("_state_update") or {}
    state_update.update(adapter_state)

    if outcome == "interrupt":
        state_update["_pending_directives"] = details.get("_pending_directives", [])
        state_update["_pending_step"] = "apply_one_job"
        return {
            "interrupt": True,
            "interrupt_reason": details.get("reason", "adapter requested interrupt"),
            "next": "apply_one_job",
            "state_update": state_update,
        }

    if outcome == "applied":
        state_update["current_url"] = None
        state_update["_zip_phase"] = None
        state_update["submitted_count"] = int(state.data.get("submitted_count", 0)) + 1
        return {
            "next": "loop_or_done",
            "state_update": state_update,
            "artifact_key": "apply_one_job",
            "artifact": {
                "outcome": "applied",
                "url": current_url,
                "ref_number": details.get("ref_number"),
                "company": details.get("company"),
                "ats": adapter_name.removeprefix("adapter_"),
            },
        }

    # skipped or failed_*
    state_update["current_url"] = None
    state_update["_zip_phase"] = None
    return {
        "next": "loop_or_done",
        "state_update": state_update,
        "artifact_key": "apply_one_job",
        "artifact": {
            "outcome": outcome,
            "url": current_url,
            "reason": details.get("reason"),
            "ats": adapter_name.removeprefix("adapter_"),
        },
    }


def _step_loop_or_done(state: SkillState, params: dict) -> dict:
    """Decision step: loop back to apply_one_job OR finish the session."""
    queue = list(state.data.get("queue") or [])
    submitted_count = int(state.data.get("submitted_count", 0))
    max_apps = int(params.get("max_applications") or DEFAULT_MAX_APPLICATIONS)
    if queue and submitted_count < max_apps:
        return {
            "next": "apply_one_job",
            "state_update": {"_loop_decision": "continue"},
        }
    return {
        "next": "log_session",
        "state_update": {
            "_loop_decision": "finalize",
            "_final_submitted_count": submitted_count,
            "_queue_remaining": len(queue),
        },
    }


def _step_log_session(state: SkillState, params: dict) -> dict:
    """INTERRUPT in v1 — Drive WRITE primitive not wired yet. Operator
    pastes the session block into the applications log manually. v2
    uses a writeable Drive endpoint when available."""
    submitted = [a for a in (state.artifacts.get("apply_one_job") or [])
                 if isinstance(a, dict) and a.get("outcome") == "applied"]
    return {
        "interrupt": True,
        "interrupt_reason": "drive_write_primitive_not_wired_in_v1",
        "next": END,
        "state_update": {
            "_pending_step": "log_session",
            "_session_block_text": _format_session_block(state, submitted, params),
        },
    }


def _format_session_block(state: SkillState, submitted: list, params: dict) -> str:
    """Produce a markdown block the operator can paste into the
    applications log under a new SESSION header."""
    lines = []
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    lines.append(f"## NEW APPLICATIONS — SESSION {today}")
    lines.append("")
    if params.get("note"):
        lines.append(f"*Note:* {params['note']}")
        lines.append("")
    if not submitted:
        lines.append("(no submissions this session)")
    else:
        for entry in submitted:
            lines.append(f"- **{entry.get('company','?')}** — "
                         f"{entry.get('role','?')} — "
                         f"Status: Submitted — "
                         f"ATS: {entry.get('ats','?')}")
    lines.append("")
    lines.append(f"_session_id: {state.session_id}_  ")
    lines.append(f"_step_count: {state.step_count}_  ")
    lines.append(f"_skipped_dedup: {len((state.data.get('skipped') or {}).get('dedup', []))}_  ")
    return "\n".join(lines)


# ─── Adapter stubs (all INTERRUPT in v1; 12-hour push fills these in) ─

def _adapter_stub(host_label: str):
    def _fn(state: SkillState, url: str, profile: dict, rules: dict) -> dict:
        return {
            "outcome": "interrupt",
            "details": {"reason": f"adapter_{host_label}_not_implemented_in_v1"},
        }
    _fn.__name__ = f"adapter_{host_label}"
    return _fn


# Background-check / fair-chance gate (Missing 5 from debug pass):
# explicit phrase matchers + skip-reason logging. Any listing whose page
# text contains one of these phrases is skipped with a recorded reason.
_BG_CHECK_PHRASES = (
    "criminal background check",
    "background check required",
    "must pass a background check",
    "pass a criminal background",
    "criminal history check",
    "subject to a background check",
    "must clear a background check",
    "background screening required",
)


def adapter_ziprecruiter(state: SkillState, url: str, profile: dict, rules: dict) -> dict:
    """ZipRecruiter adapter — phase-tracked, single-directive-per-turn.

    Phases:
      nav            → emit BROWSER_NAV+WAIT+READ_PAGE → INTERRUPT
      find_apply     → consume page tree, check skip filters, find Apply →
                       emit BROWSER_FIND → INTERRUPT (or skip outcome)
      operator_review → INTERRUPT for operator review (v1 stops here).

    v2 will extend with click_apply / fill_form / submit_gate / verify.
    Phase tracker lives in state.data['_zip_phase']."""
    phase = state.data.get("_zip_phase") or "nav"
    last = state.data.get("_last_directive_results", "") or ""

    if phase == "nav":
        return {
            "outcome": "interrupt",
            "details": {
                "_pending_directives": [
                    f"BROWSER_NAV: {url}",
                    "BROWSER_WAIT: 3000",
                    "BROWSER_READ_PAGE: main",
                ],
                "reason": f"adapter_ziprecruiter: navigating to listing",
                "_state_update": {"_zip_phase": "find_apply"},
            },
        }

    if phase == "find_apply":
        low = last.lower()

        for skip_co in (rules.get("skip_companies") or []):
            if skip_co.lower() in low:
                return {
                    "outcome": "skipped",
                    "details": {"reason": f"skip_company:{skip_co}"},
                }

        for phrase in _BG_CHECK_PHRASES:
            if phrase in low:
                return {
                    "outcome": "skipped",
                    "details": {"reason": f"background_check:{phrase}"},
                }

        if "apply" not in low:
            return {
                "outcome": "skipped",
                "details": {"reason": "no_apply_button_on_page"},
            }

        return {
            "outcome": "interrupt",
            "details": {
                "_pending_directives": ["BROWSER_FIND: Apply"],
                "reason": "adapter_ziprecruiter: locating Apply button selector",
                "_state_update": {"_zip_phase": "operator_review"},
            },
        }

    if phase == "operator_review":
        return {
            "outcome": "interrupt",
            "details": {
                "reason": (
                    "adapter_ziprecruiter v1 scope reached: Apply button located. "
                    "Operator-driven form-fill from here. v2 will implement: "
                    "click Apply → parse form tree → fill from profile → "
                    "review gate → BROWSER_SUBMIT → capture ref number."
                ),
                "_state_update": {"_zip_phase": "v1_scope_end"},
            },
        }

    return {
        "outcome": "skipped",
        "details": {"reason": f"unknown_zip_phase:{phase}"},
    }


def adapter_indeed(state: SkillState, url: str, profile: dict, rules: dict) -> dict:
    """Indeed adapter — phase-tracked, single-directive-per-turn.

    Structural parallel to adapter_ziprecruiter (lines 485-562). v1 scope
    reaches the Apply button locate phase and stops at operator_review for
    operator-driven form-fill. v2 wiring (TODO markers below) extends to
    full fill + upload + submit-gate.

    Phases:
      nav             → emit BROWSER_NAV+WAIT+READ_PAGE → INTERRUPT
      find_apply      → consume page tree, check skip filters, find Apply
                        button → emit BROWSER_FIND → INTERRUPT (or skip
                        outcome on filter hit)
      operator_review → INTERRUPT for operator review (v1 stops here);
                        names the resume_path the operator will need for
                        v2 upload phase so the chain is visible.

    Phase tracker lives in state.data['_indeed_phase']. Mirrors the
    ZipRecruiter pattern intentionally — when v2 lands fill_form /
    upload_resume / submit_gate phases, both adapters extend in parallel
    so the per-host code stays comparable.

    v2 phases to add (each requires LIVE Indeed page inspection for
    selectors — DO NOT guess; the operator captures selectors with
    DevTools and pastes them before this code is written):
      click_apply     → BROWSER_CLICK: <apply-button-selector>  # TODO
      read_form       → BROWSER_WAIT + BROWSER_READ_PAGE: form  # TODO
      fill_form       → BROWSER_FILL chain from profile fields, e.g.
                        BROWSER_FILL: input[name="firstName"] :: <profile.full_name first part>
                        BROWSER_FILL: input[name="email"] :: <profile.email>
                        BROWSER_FILL: input[name="phone"] :: <profile.phone>
                        BROWSER_FILL: input[name="city"] :: <profile.address city>
                        (selectors TODO from live Indeed page)
      upload_resume   → BROWSER_UPLOAD_FILE: <file-input-selector> :: <resume_path>
                        resume_path comes from params (already in
                        SkillState.params per skill_runtime.py:133 +
                        line 408 — no plumbing change needed). The
                        extension's CDP DOM.setFileInputFiles bridge at
                        sensei_extension/service_worker.js:661-700 is
                        already wired; it just needs the right selector.
      submit_gate     → INTERRUPT for operator final-confirm before
                        BROWSER_CLICK: <submit-button-selector>  # TODO
      verify          → BROWSER_READ_PAGE: main + parse for confirmation
                        text / ref number → outcome "applied" with
                        ref_number in details.
    """
    phase = state.data.get("_indeed_phase") or "nav"
    last = state.data.get("_last_directive_results", "") or ""

    if phase == "nav":
        return {
            "outcome": "interrupt",
            "details": {
                "_pending_directives": [
                    f"BROWSER_NAV: {url}",
                    "BROWSER_WAIT: 3000",
                    "BROWSER_READ_PAGE: main",
                ],
                "reason": "adapter_indeed: navigating to listing",
                "_state_update": {"_indeed_phase": "find_apply"},
            },
        }

    if phase == "find_apply":
        low = last.lower()

        # Skip filters (same as ziprecruiter) — keep these BEFORE the
        # Apply-button lookup so a skip-company or background-check
        # listing never burns the next round on locating its button.
        for skip_co in (rules.get("skip_companies") or []):
            if skip_co.lower() in low:
                return {
                    "outcome": "skipped",
                    "details": {"reason": f"skip_company:{skip_co}"},
                }

        for phrase in _BG_CHECK_PHRASES:
            if phrase in low:
                return {
                    "outcome": "skipped",
                    "details": {"reason": f"background_check:{phrase}"},
                }

        # Indeed-specific Apply detection. Common copy:
        #   "Apply now", "Apply on company site", "Easy Apply",
        #   "Continue your application", "Apply"
        # The string "apply" appears in all of them. Listings that don't
        # offer Apply (e.g. expired / external-only with no embed) get
        # skipped with an explicit reason.
        if "apply" not in low:
            return {
                "outcome": "skipped",
                "details": {"reason": "no_apply_button_on_page"},
            }

        is_indeed_hosted = "apply with indeed" in low or "indeed apply" in low
        is_external_only = ("apply on company site" in low) and not is_indeed_hosted

        if is_external_only:
            return {
                "outcome": "skipped",
                "details": {"reason": "external_apply_only"},
            }

        if is_indeed_hosted:
            return {
                "outcome": "interrupt",
                "details": {
                    "_pending_directives": [
                        "BROWSER_CLICK: #indeedApplyButton",
                        "BROWSER_WAIT: 5000",
                        "BROWSER_READ_PAGE: main",
                    ],
                    "reason": "adapter_indeed: clicking Apply with Indeed, navigating to smartapply.indeed.com",
                    "_state_update": {"_indeed_phase": "read_form"},
                },
            }

        return {
            "outcome": "interrupt",
            "details": {
                "_pending_directives": ["BROWSER_FIND: Apply"],
                "reason": "adapter_indeed: locating Apply button selector",
                "_state_update": {"_indeed_phase": "operator_review"},
            },
        }

    if phase == "operator_review":
        # v1 stops here. Name the resume_path the operator's session is
        # carrying so the chain to v2 upload is visible — when v2 lands,
        # this is the param fill_form / upload_resume will read.
        resume_path = state.params.get("resume_path") or "(not provided)"
        return {
            "outcome": "interrupt",
            "details": {
                "reason": (
                    "adapter_indeed v1 scope reached: Apply button located. "
                    "Operator-driven form-fill from here. v2 will implement: "
                    "click Apply → parse form tree → fill from profile → "
                    f"upload résumé (resume_path: {resume_path}) via "
                    "BROWSER_UPLOAD_FILE → review gate → BROWSER_CLICK on "
                    "submit-button-selector → capture ref number. Selectors "
                    "for fill/upload/submit require live Indeed page "
                    "inspection — do NOT write blind."
                ),
                "_state_update": {"_indeed_phase": "v1_scope_end"},
            },
        }

    if phase == "read_form":
        return {
            "outcome": "interrupt",
            "details": {
                "reason": (
                    "adapter_indeed v2 click_apply complete; on "
                    "smartapply.indeed.com form. Form-side selectors not yet "
                    "captured — operator-driven fill from here. Next pass wires "
                    "fill_form / upload_resume / submit_gate."
                ),
                "_state_update": {"_indeed_phase": "v2_form_pending"},
            },
        }

    return {
        "outcome": "skipped",
        "details": {"reason": f"unknown_indeed_phase:{phase}"},
    }

adapter_workday = _adapter_stub("workday")
adapter_greenhouse = _adapter_stub("greenhouse")
adapter_lever = _adapter_stub("lever")
adapter_ashby = _adapter_stub("ashby")
adapter_icims = _adapter_stub("icims")
adapter_custom = _adapter_stub("custom")


# ─── Skill definition (read by skill_runtime.load_skill) ────────────

STEPS = [
    Step("load_drive_refs", _step_load_drive_refs,
         description="Read ~/.master_ai_drive_refs.json"),
    Step("load_profile", _step_load_profile,
         description="Read ~/.master_ai_profile.json + soft-prereq warnings"),
    Step("fetch_ai_query_spec", _step_fetch_ai_query_spec,
         description="BROWSER_NAV + BROWSER_READ_PAGE on AI Query doc /mobilebasic URL",
         retry_on_fail=2, recovery_next=INTERRUPT),
    Step("fetch_applications_log", _step_fetch_applications_log,
         description="BROWSER_NAV + BROWSER_READ_PAGE on applications-log /mobilebasic URL",
         retry_on_fail=2, recovery_next=INTERRUPT),
    Step("reconcile_inbox", _step_reconcile_inbox,
         description="Scan Gmail + AOL inboxes for confirmation emails matching Submitted entries"),
    Step("enumerate_candidate_jobs", _step_enumerate_candidate_jobs,
         description="Filter candidate_urls against dedup + hard stops + residential rule"),
    Step("apply_one_job", _step_apply_one_job,
         description="Pop one URL from queue, dispatch to per-host adapter, INTERRUPT before BROWSER_SUBMIT",
         retry_on_fail=1, recovery_next="loop_or_done"),
    Step("loop_or_done", _step_loop_or_done,
         description="Decide: continue queue OR finalize session"),
    Step("log_session", _step_log_session,
         description="Append session block to applications log (Drive write or operator paste)"),
]

ENTRYPOINT = "load_drive_refs"
