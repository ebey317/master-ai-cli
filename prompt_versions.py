"""prompt_versions.py — content-hash-first prompt version stamping.

Phase 1 of the Reactive Waddling Papert plan
(~/.claude/plans/reactive-waddling-papert.md).

Every audit row should be traceable back to the exact prompt text that
produced the directive. Content-hash is the source of truth (survives the
dirty-tree case where the working copy differs from the most recent
commit); git-short is paired when available for human readability.

The intended pattern in master_ai.py: at every system-prompt assembly site
(orchestrate(), CLOUD_SYSTEM construction, etc.), call `stamp(assembled)`.
Audit writers then call `current()` to read the most recently stamped
version dict.

Cached internally by content hash so repeated calls with the same prompt
text don't re-hash or re-fork git.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

_GIT_DIR = Path(__file__).resolve().parent  # ~/scripts


def _git_commit_short() -> str | None:
    """Return the 10-char short HEAD, or None if git is unavailable / errors.

    Bounded by a 2s subprocess timeout. Failures (no git, not a repo, etc.)
    return None silently so prompt versioning never blocks prompt assembly.
    """
    try:
        cp = subprocess.run(
            ["git", "-C", str(_GIT_DIR), "rev-parse", "--short=10", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if cp.returncode == 0:
            short = cp.stdout.strip()
            return short or None
    except Exception:
        pass
    return None


def _prompt_sha256_short(assembled_prompt_text: str) -> str:
    h = hashlib.sha256(
        (assembled_prompt_text or "").encode("utf-8", errors="ignore")
    ).hexdigest()
    return h[:12]


_CACHE: dict[str, dict] = {}


def compute_prompt_version(assembled_prompt_text: str) -> dict:
    """Compute the full version dict for one assembled prompt string.

    Returned dict carries:
      - prompt_sha256_short: 12-char content hash (source of truth)
      - git_commit_short: 10-char HEAD short, or None when unavailable
      - registry_version: capabilities.REGISTRY_VERSION
      - safety_policy_version: capabilities.SAFETY_POLICY_VERSION
      - prompt_version: combined "{registry_version}+{sha_short}" string,
        used as the canonical column in audit rows.
    """
    sha_short = _prompt_sha256_short(assembled_prompt_text or "")
    if sha_short in _CACHE:
        return _CACHE[sha_short]

    # Lazy import — capabilities.py is the source of registry/safety versions
    # but we don't want a hard dependency at module-load time (avoids cycles
    # if capabilities.py ever needs anything from prompt_versions).
    try:
        import capabilities  # type: ignore

        registry_version = capabilities.REGISTRY_VERSION
        safety_policy_version = capabilities.SAFETY_POLICY_VERSION
    except Exception:
        registry_version = "unknown"
        safety_policy_version = "unknown"

    git_short = _git_commit_short()

    result = {
        "prompt_sha256_short": sha_short,
        "git_commit_short": git_short,
        "registry_version": registry_version,
        "safety_policy_version": safety_policy_version,
        "prompt_version": f"{registry_version}+{sha_short}",
    }
    _CACHE[sha_short] = result
    return result


# Module-level "current" pointer — audit writers read this between calls
# without needing the original prompt text in scope.
_LAST_VERSION: dict | None = None


def stamp(assembled_prompt_text: str) -> dict:
    """Compute version for assembled_prompt_text AND set as current.

    Called at every system-prompt assembly site in master_ai.py.
    """
    global _LAST_VERSION
    _LAST_VERSION = compute_prompt_version(assembled_prompt_text)
    return _LAST_VERSION


def current() -> dict:
    """Return the most recently stamped version dict.

    If nothing has been stamped yet, returns a sentinel dict with
    'unstamped' tags so audit rows are still well-formed JSON.
    """
    if _LAST_VERSION is None:
        return {
            "prompt_sha256_short": None,
            "git_commit_short": _git_commit_short(),
            "registry_version": "unstamped",
            "safety_policy_version": "unstamped",
            "prompt_version": "unstamped",
        }
    return _LAST_VERSION


__all__ = ["compute_prompt_version", "stamp", "current"]
