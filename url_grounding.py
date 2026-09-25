"""Deterministic URL grounding for "open <site|domain|url>" intents.

This keeps common "open X" requests out of the model path so we don't
hallucinate URLs. It is intentionally small and conservative: return a URL
only when we can ground it confidently.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_OPEN_PREFIX_RE = re.compile(
    r"^\s*(?:open|go\s+to|navigate\s+to|visit|launch)\s+(.+?)\s*$",
    re.IGNORECASE,
)


_TRAILING_PUNCT_RE = re.compile(r"[)\].,!?;:]+$")


_SITE_ALIASES = {
    # High-frequency sites
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google drive": "https://drive.google.com",
    "drive": "https://drive.google.com",
    "google docs": "https://docs.google.com",
    "docs": "https://docs.google.com",
    "indeed": "https://www.indeed.com",
    "ziprecruiter": "https://www.ziprecruiter.com",
    "glassdoor": "https://www.glassdoor.com",
    "linkedin": "https://www.linkedin.com",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
}


def _clean_target(text: str) -> str:
    s = (text or "").strip().strip("\"'`")
    s = _TRAILING_PUNCT_RE.sub("", s)
    return s.strip()


def _looks_like_domain(s: str) -> bool:
    if not s or " " in s:
        return False
    if s.startswith(("http://", "https://")):
        return False
    # Very small, conservative heuristic: has a dot and only domain-ish chars.
    if "." not in s:
        return False
    if not re.match(r"^[A-Za-z0-9.-]+$", s):
        return False
    return True


def resolve_open_target_url(user_text: str) -> str | None:
    """Return a grounded URL for an "open ..." style request, else None."""
    m = _OPEN_PREFIX_RE.match(user_text or "")
    if not m:
        return None
    target = _clean_target(m.group(1))
    if not target:
        return None

    low = target.lower()
    if low in _SITE_ALIASES:
        return _SITE_ALIASES[low]

    # Handle a bare URL (with scheme).
    if low.startswith(("http://", "https://")):
        try:
            parsed = urlparse(target)
            return target if parsed.scheme and parsed.netloc else None
        except Exception:
            return None

    # Handle common alias forms with minor punctuation differences.
    low_norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", low)).strip()
    if low_norm in _SITE_ALIASES:
        return _SITE_ALIASES[low_norm]

    # Handle bare domains like "example.com".
    if _looks_like_domain(target):
        return "https://" + target

    return None
