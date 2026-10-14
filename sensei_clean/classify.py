"""Shared token matching for adapter classifiers.

2026-10-13: the adapter token lists ("cv", "tax", "resume", ...) used to
be matched as raw substrings against the whole path. That poisoned an
entire tree whenever an ancestor directory name happened to contain a
token: a random tempfile dir like /tmp/tmp65avurcv contains "cv", which
classified every file under it as career-sensitive (monitored lane,
confidence 0.7) and silently dropped its unattended actions; a folder
named "taxonomy" matched "tax" the same way. Match on word boundaries
instead: a token must start at a non-alphanumeric boundary and end at
one, with an optional plural suffix ("s" or "es") so common folder names
("Resumes", "Taxes", "pay stubs") still match. "mycv" and "presumed"
(substring matches) intentionally no longer fire; the file still
classifies by mime, so nothing is lost that matters.

Callers may pass full paths, path segments, or bare names; '/', '-',
'_', '.', and ' ' all count as word boundaries because the boundary
classes only contain [a-z0-9].
"""

from __future__ import annotations

import re
from functools import cache


@cache
def _token_re(token: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(token)}(?:es|s)?(?![a-z0-9])")


def matches_tokens(text: str, tokens: tuple[str, ...]) -> bool:
    """True when any token appears as a word in ``text`` (case-insensitive)."""
    lower = text.lower()
    return any(_token_re(token).search(lower) for token in tokens)
