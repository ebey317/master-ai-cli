"""ats_fingerprint.py — Score-based ATS detection from a DOM snapshot.

Returns one of: "greenhouse" | "lever" | "workday" | "unknown"
Requires at least MIN_SCORE hits before committing to a label.
Designed to be called with either raw HTML text or a URL string.

Usage:
    from ats_fingerprint import fingerprint_ats
    ats = fingerprint_ats(html_text, url="https://boards.greenhouse.io/company/jobs/123")
    # → "greenhouse"
"""

from __future__ import annotations

import re

# Minimum number of signature hits needed to commit to an ATS label.
# Avoids false positives from single coincidental matches.
MIN_SCORE = 2

# ── Signature tables ──────────────────────────────────────────────────────────
# Each entry is (pattern, weight). Patterns are matched against the HTML +
# URL concatenation (case-insensitive). Weight is always 1 here — reserved
# for future confidence scoring.

_GREENHOUSE_SIGS: list[tuple[str, int]] = [
    # URL patterns
    (r"boards\.greenhouse\.io",             1),
    (r"app\.greenhouse\.io",                1),
    (r"greenhouse\.io",                     1),
    # Form / input name patterns
    (r'name=["\']job_application\[',        1),
    (r'id=["\']main-application-form',      1),
    (r'class=["\'][^"\']*application-form', 1),
    # Hidden fields greenhouse always injects
    (r'name=["\']authenticity_token',       1),
    # Typical Greenhouse submit button
    (r'id=["\']submit_app',                 1),
    # Greenhouse CSS / JS asset paths
    (r'grnh\.se',                           1),
    (r'greenhouse-io\.',                    1),
    # Greenhouse data attributes
    (r'data-source=["\']greenhouse',        1),
]

_LEVER_SIGS: list[tuple[str, int]] = [
    # URL patterns — broad first (catches any lever.co subdomain),
    # specific second (jobs.lever.co or app.lever.co add a second hit).
    (r"lever\.co",                          1),
    (r"jobs\.lever\.co",                    1),
    (r"app\.lever\.co",                     1),
    # Form action pointing to lever
    (r'action=["\'][^"\']*lever\.co',       1),
    # Lever-specific class names
    (r'class=["\'][^"\']*application-form', 1),  # shared with greenhouse; needs URL anchor
    (r'class=["\'][^"\']*lever-',           1),
    # Lever hidden inputs
    (r'name=["\']lever-',                   1),
    # Lever resume dropzone
    (r'id=["\']lever-resume',               1),
    # Lever source tag
    (r'data-qa=["\']lever-',               1),
]

_WORKDAY_SIGS: list[tuple[str, int]] = [
    # URL patterns — broad first (any workday domain),
    # specific second (tenant-format URLs add a second hit).
    (r"workday",                            1),
    (r"myworkdayjobs\.com",                 1),
    (r"wd[0-9]+\.myworkday\.com",           1),
    (r"workday\.com/[^/]+/d/",             1),
    # Workday's canonical automation attribute
    (r'data-automation-id=["\']',           1),
    # Workday React shell
    (r'class=["\'][^"\']*WDAY-',           1),
    (r'class=["\'][^"\']*wd-',             1),
    # Workday's tenant-specific namespace patterns
    (r'data-uxi-widget-type',              1),
    # Workday JS bundle
    (r'workday-web-sdk',                   1),
]

# ── Core scorer ───────────────────────────────────────────────────────────────

def _score(corpus: str, sigs: list[tuple[str, int]]) -> int:
    """Return total weight of matched signatures."""
    total = 0
    for pattern, weight in sigs:
        if re.search(pattern, corpus, re.IGNORECASE):
            total += weight
    return total


def fingerprint_ats(html: str, url: str = "") -> str:
    """Identify the ATS powering a job application page.

    Args:
        html: Raw HTML of the page (or any DOM text dump).
        url:  The page URL — included in the scored corpus for URL-based signals.

    Returns:
        One of: "greenhouse" | "lever" | "workday" | "unknown"
        "unknown" if no ATS scores >= MIN_SCORE.
    """
    if not html and not url:
        return "unknown"

    corpus = (url or "") + "\n" + (html or "")

    gh_score = _score(corpus, _GREENHOUSE_SIGS)
    lv_score = _score(corpus, _LEVER_SIGS)
    wd_score = _score(corpus, _WORKDAY_SIGS)

    scores = {
        "greenhouse": gh_score,
        "lever":      lv_score,
        "workday":    wd_score,
    }

    best_ats, best_score = max(scores.items(), key=lambda kv: kv[1])

    if best_score < MIN_SCORE:
        return "unknown"

    # Tie-break: if two ATS scores are equal, default to "unknown" so we
    # don't silently pick the wrong map dictionary.
    second_best = sorted(scores.values(), reverse=True)[1]
    if best_score == second_best:
        return "unknown"

    return best_ats


# ── CLI test harness ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: ats_fingerprint.py <html_file_or_url> [url]")
        sys.exit(1)

    source = sys.argv[1]
    url_hint = sys.argv[2] if len(sys.argv) > 2 else ""

    # If the argument looks like a URL, score it without reading a file.
    if source.startswith("http://") or source.startswith("https://"):
        result = fingerprint_ats("", url=source)
    else:
        try:
            html_text = open(source, encoding="utf-8", errors="replace").read()
        except FileNotFoundError:
            print(f"File not found: {source}")
            sys.exit(1)
        result = fingerprint_ats(html_text, url=url_hint)

    print(result)
