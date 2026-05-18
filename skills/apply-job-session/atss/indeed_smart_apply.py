"""Indeed Smart Apply (smartapply.indeed.com) selector config.

captured_on:   pending — first capture session
captured_by:   operator + browser-Claude walkthrough
captured_from: one real Indeed Easy Apply listing the operator selects
last_verified: pending

Status: SCHEMA LOCKED, SELECTORS PENDING capture-session.

Read the SAFETY CLAUSE in atss/__init__.py before extending this file.
This config is static data consumed by adapter_indeed v2's fill_form
phase. It does not authorize autonomous submission — human-in-loop on
every irreversible branch is non-negotiable per the executor framework.
"""

from __future__ import annotations

# ─── Field role → sensitivity tier ────────────────────────────────────
# Executor reads this dict to assign sensitivity. Static across ATSs;
# the per-ATS configs only name field_role, sensitivity is centralized
# here. Per browser-Claude design review 2026-05-18 — sensitivity rules
# live in ONE place (the executor), adapters stay purely structural.

FIELD_ROLE_TO_SENSITIVITY = {
    # none — public/innocuous
    "name_first":          "none",
    "name_last":           "none",
    "name_preferred":      "none",
    "name_full":           "none",
    "experience_years":    "none",
    "education_level":     "none",
    "work_history":        "none",
    "cover_letter":        "none",
    "linkedin_url":        "none",
    "resume_upload":       "none",

    # personal — contact info, fingerprint-redacted in audit log
    "email":               "personal",
    "phone":               "personal",
    "address_line_1":      "personal",
    "address_line_2":      "personal",
    "city":                "personal",
    "state":               "personal",
    "zip":                 "personal",
    "country":             "personal",
    "date_of_birth":       "personal",

    # financial — refuse-sensitive, human keypress only
    "salary_expectation":  "financial",
    "bank_routing":        "financial",
    "bank_account":        "financial",

    # government_id — refuse-sensitive, human keypress only
    "ssn":                 "government_id",
    "ein":                 "government_id",
    "passport_number":     "government_id",
    "drivers_license":     "government_id",
    "immigration_number":  "government_id",
    "work_authorization_id": "government_id",
}


# ─── Per-field selectors ──────────────────────────────────────────────
# Each entry: {field_role, label_visible, css_selector, html_input_type,
#              required, step_index, options_if_select?}
#
# field_role: from FIELD_ROLE_TO_SENSITIVITY keys above. Adapter never
#             specifies sensitivity — executor derives it from role.
# css_selector: prefer #id > [name=...] > [data-*]; AVOID .css-hashed
#               class names (Indeed regenerates them).
# options_if_select: list of option text values for select/radio fields
#                    so the matcher knows the allowed set without re-reading
#                    the DOM. Omit for text/textarea/file inputs.
#
# PENDING — to be filled in by operator + browser-Claude walkthrough.
# Each entry the operator captures gets appended here with the date in
# the entry comment.

FIELDS: list[dict] = [
    # ─── To be captured ───────────────────────────────────────────────
    # Example shape (do not commit fake selectors — fill in after live
    # walkthrough; remove this example when first real entry lands):
    #
    # {
    #     "field_role": "name_first",
    #     "label_visible": "First Name",
    #     "css_selector": "input#first-name-input",
    #     "html_input_type": "text",
    #     "required": True,
    #     "step_index": 1,
    # },
]


# ─── Submit/Continue/file-upload selectors ───────────────────────────

CONTINUE_BUTTON_SELECTOR: str | None = None
SUBMIT_BUTTON_SELECTOR: str | None = None
RESUME_UPLOAD_INPUT_SELECTOR: str | None = None
CONFIRMATION_INDICATOR_SELECTOR: str | None = None
CONFIRMATION_TEXT_PATTERN: str | None = None  # regex match on page text
