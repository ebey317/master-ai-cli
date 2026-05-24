"""ats_maps/lever.py — Lever ATS selector → profile key mapping.

Minimal viable Lever map. Lever's form structure is more consistent than
Greenhouse across companies — the main form is always at .application-form
with input names like "name", "email", "phone", "org", "urls[LinkedIn]".

See: https://hire.lever.co — Lever Hire ATS documentation.
"""

from __future__ import annotations

SELECTORS: dict[str, str] = {

    # ── Personal: Name ────────────────────────────────────────────────────────
    'input[name="name"]':                           "personal.full_name",
    'input[id="name"]':                             "personal.full_name",
    'input[autocomplete="name"]':                   "personal.full_name",
    # Some Lever postings split name
    'input[name="first_name"]':                     "personal.first_name",
    'input[name="last_name"]':                      "personal.last_name",

    # ── Contact ───────────────────────────────────────────────────────────────
    'input[name="email"]':                          "personal.email",
    'input[type="email"]':                          "personal.email",

    'input[name="phone"]':                          "personal.phone",
    'input[type="tel"]':                            "personal.phone",

    # ── Current employer / headline ───────────────────────────────────────────
    'input[name="org"]':                            "experience.0.employer",
    'input[id="org"]':                              "experience.0.employer",

    # ── Links ─────────────────────────────────────────────────────────────────
    'input[name="urls[LinkedIn]"]':                 "personal.linkedin",
    'input[name*="linkedin" i]':                    "personal.linkedin",
    'input[placeholder*="linkedin" i]':             "personal.linkedin",

    'input[name="urls[GitHub]"]':                   "personal.github",
    'input[name*="github" i]':                      "personal.github",

    'input[name="urls[Portfolio]"]':                "personal.website",
    'input[name="urls[Website]"]':                  "personal.website",
    'input[name*="website" i]':                     "personal.website",

    # ── Resume upload ─────────────────────────────────────────────────────────
    'input[type="file"]':                           "documents.resume_url",
    'input[type="file"][id*="resume"]':             "documents.resume_url",
    'input[type="file"][data-qa*="resume"]':        "documents.resume_url",

    # ── Cover letter (textarea) ───────────────────────────────────────────────
    'textarea[name="comments"]':                    "custom_answers.cover_letter_template",
    'textarea[id="comments"]':                      "custom_answers.cover_letter_template",
    'textarea[placeholder*="cover letter" i]':      "custom_answers.cover_letter_template",

    # ── Work authorization (Lever EEO / compliance section) ───────────────────
    # Lever renders these as radio groups with data-qa attributes.
    # The autofill code matches radio by value text, not selector value.
    'input[type="radio"][data-qa*="authorized"]':   "disclosures.authorized_to_work",
    'input[type="radio"][data-qa*="sponsorship"]':  "disclosures.requires_sponsorship",

    # ── EEOC / diversity dropdowns ────────────────────────────────────────────
    'select[data-qa*="gender"]':                    "disclosures.gender",
    'select[data-qa*="race"]':                      "disclosures.race_ethnicity",
    'select[data-qa*="ethnicity"]':                 "disclosures.race_ethnicity",
    'select[data-qa*="veteran"]':                   "disclosures.veteran_status",
    'select[data-qa*="disability"]':                "disclosures.disability_status",
}

STANDARD_FIELDS: frozenset[str] = frozenset(SELECTORS.keys())

FIELD_TYPES: dict[str, str] = {
    "documents.resume_url":             "file",
    "disclosures.authorized_to_work":   "radio",
    "disclosures.requires_sponsorship": "radio",
    "disclosures.gender":               "select",
    "disclosures.race_ethnicity":       "select",
    "disclosures.veteran_status":       "select",
    "disclosures.disability_status":    "select",
    "custom_answers.cover_letter_template": "textarea",
}
