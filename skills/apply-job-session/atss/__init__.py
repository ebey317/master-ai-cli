"""Per-ATS selector configs for the apply-job-session adapter library.

Each file in this directory documents the structural selectors of one
ATS's apply flow (Indeed Smart Apply, Workday, LinkedIn Easy Apply, etc.).

SAFETY CLAUSE — read before using or extending any file in this directory:

These configs are captured by walking real flows with a human operator
in the loop. They reflect each form's structure as of the capture date
recorded in each config's `captured_on` field. They are static data
consumed by the adapter runtime; they do NOT constitute permission or
capability for autonomous, unattended application submission.

Every adapter that reads these configs MUST require human-in-loop on
irreversible branches:
- CAPTCHA solving — human keypress
- Sensitive field fills (sensitivity tier above `personal` per the
  executor framework) — human keypress
- Final submit click — human keypress

The selectors will drift as ATSs update their pages. Stale selectors are
expected. Re-capture sessions (one flow per session, with human in loop)
are the maintenance path. Do NOT script multi-listing serial captures —
that's a scraping pattern that gets accounts flagged and crosses from
documenting-one-flow-with-the-operator into automated reconnaissance.

Per browser-Claude design review 2026-05-18.
"""
