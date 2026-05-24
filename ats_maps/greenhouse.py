"""ats_maps/greenhouse.py — Greenhouse ATS selector → profile key mapping.

Each entry maps a CSS selector (as it appears in Greenhouse job application
forms) to a dot-notation key path in ~/.master_ai_profile.json.

The autofill_job_form tool resolves key paths like:
    "personal.first_name"  →  profile["personal"]["first_name"]
    "disclosures.authorized_to_work"  →  profile["disclosures"]["authorized_to_work"]

Selector variants are listed from most-specific to least-specific.
The autofill code tries each in order and stops at the first element found.

GREENHOUSE FORM CONVENTIONS (as of 2025):
  - Standard inputs: name="job_application[field]"
  - Required marker: aria-required="true"
  - Resume upload: input[type=file][name*="resume"]
  - Work auth dropdowns: name="job_application[answers_attributes][N][text_value]"
    paired with a label containing "authorized to work" or "sponsorship"
  - Diversity / EEO section lives inside #eeoc_fields or .eeoc-fields
"""

from __future__ import annotations

# ── Primary selector map ──────────────────────────────────────────────────────
# Format: { "css_selector": "profile.key.path" }
# Autofill code iterates this dict, resolves each selector, and sets the value.

SELECTORS: dict[str, str] = {

    # ── Personal: Name ────────────────────────────────────────────────────────
    'input[name="job_application[first_name]"]':    "personal.first_name",
    'input[id*="first_name"]':                      "personal.first_name",
    'input[autocomplete="given-name"]':             "personal.first_name",

    'input[name="job_application[last_name]"]':     "personal.last_name",
    'input[id*="last_name"]':                       "personal.last_name",
    'input[autocomplete="family-name"]':            "personal.last_name",

    # Full name fallback (some Greenhouse variants use a single field)
    'input[name="job_application[full_name]"]':     "personal.full_name",
    'input[id*="full_name"]':                       "personal.full_name",
    'input[autocomplete="name"]':                   "personal.full_name",

    # ── Contact ───────────────────────────────────────────────────────────────
    'input[name="job_application[email]"]':         "personal.email",
    'input[type="email"]':                          "personal.email",
    'input[autocomplete="email"]':                  "personal.email",

    'input[name="job_application[phone]"]':         "personal.phone",
    'input[type="tel"]':                            "personal.phone",
    'input[autocomplete="tel"]':                    "personal.phone",

    # ── Location ──────────────────────────────────────────────────────────────
    'input[name="job_application[location]"]':      "personal.city",
    'input[id*="candidate_location"]':              "personal.city",
    'input[autocomplete="address-level2"]':         "personal.city",

    # ── Links ─────────────────────────────────────────────────────────────────
    'input[name="job_application[urls][LinkedIn]"]':    "personal.linkedin",
    'input[name*="linkedin"]':                          "personal.linkedin",
    'input[id*="linkedin"]':                            "personal.linkedin",
    'input[placeholder*="linkedin" i]':                 "personal.linkedin",

    'input[name="job_application[urls][Website]"]':     "personal.website",
    'input[name*="website"]':                           "personal.website",
    'input[id*="website"]':                             "personal.website",

    'input[name="job_application[urls][GitHub]"]':      "personal.github",
    'input[name*="github"]':                            "personal.github",
    'input[id*="github"]':                              "personal.github",

    # ── Work Authorization ────────────────────────────────────────────────────
    # These are dropdowns — value must match an <option> text.
    # Option B (validation-based): autofill code attempts this value,
    # then confirms against live <option> list before setting.
    'select[name*="authorized_to_work"]':               "disclosures.authorized_to_work",
    'select[id*="authorized_to_work"]':                 "disclosures.authorized_to_work",

    'select[name*="require_sponsorship"]':              "disclosures.requires_sponsorship",
    'select[name*="visa_sponsorship"]':                 "disclosures.visa_sponsorship",
    'select[id*="require_sponsorship"]':                "disclosures.requires_sponsorship",

    # ── Resume upload ─────────────────────────────────────────────────────────
    # The autofill code detects type=file and uses documents.resume_url
    # to upload via the extension's file-upload pathway (not a text set).
    'input[type="file"][name*="resume"]':               "documents.resume_url",
    'input[type="file"][id*="resume"]':                 "documents.resume_url",
    'input[type="file"]':                               "documents.resume_url",

    # ── EEO / Diversity fields ────────────────────────────────────────────────
    # Inside #eeoc_fields or .eeoc-fields; these are dropdowns.
    '#eeoc_fields select[name*="gender"]':              "disclosures.gender",
    '.eeoc-fields select[name*="gender"]':              "disclosures.gender",
    'select[id*="gender"]':                             "disclosures.gender",

    '#eeoc_fields select[name*="race"]':                "disclosures.race_ethnicity",
    '.eeoc-fields select[name*="race"]':                "disclosures.race_ethnicity",
    'select[id*="race"]':                               "disclosures.race_ethnicity",
    'select[id*="ethnicity"]':                          "disclosures.race_ethnicity",

    '#eeoc_fields select[name*="veteran"]':             "disclosures.veteran_status",
    '.eeoc-fields select[name*="veteran"]':             "disclosures.veteran_status",
    'select[id*="veteran"]':                            "disclosures.veteran_status",

    '#eeoc_fields select[name*="disability"]':          "disclosures.disability_status",
    '.eeoc-fields select[name*="disability"]':          "disclosures.disability_status",
    'select[id*="disability"]':                         "disclosures.disability_status",

    # ── Screener / demographic checkboxes ─────────────────────────────────────
    'input[type="radio"][value*="authorized" i]':       "disclosures.authorized_to_work",
    'input[type="radio"][value*="sponsorship" i]':      "disclosures.requires_sponsorship",

    # ── Cover letter (textarea) ───────────────────────────────────────────────
    'textarea[name*="cover_letter"]':                   "custom_answers.cover_letter_template",
    'textarea[id*="cover_letter"]':                     "custom_answers.cover_letter_template",
}


# ── Standard-field set ────────────────────────────────────────────────────────
# Selectors whose values can be filled programmatically (code-fill phase).
# Selectors NOT in this set get routed to the LLM custom-question phase.
STANDARD_FIELDS: frozenset[str] = frozenset(SELECTORS.keys())


# ── Field type overrides ──────────────────────────────────────────────────────
# When the autofill code needs to know how to set a field (text vs select vs
# file vs radio), it checks here first, then falls back to DOM type attribute.
FIELD_TYPES: dict[str, str] = {
    "documents.resume_url":             "file",
    "disclosures.authorized_to_work":   "select",
    "disclosures.requires_sponsorship": "select",
    "disclosures.visa_sponsorship":     "select",
    "disclosures.gender":               "select",
    "disclosures.race_ethnicity":       "select",
    "disclosures.veteran_status":       "select",
    "disclosures.disability_status":    "select",
    "custom_answers.cover_letter_template": "textarea",
}
