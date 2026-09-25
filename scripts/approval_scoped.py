"""Per-profile command allowlist.

Instead of a single module-level allowlist, each routed profile keeps its own
persistent allowlist under its own home directory. Single-profile processes keep
the legacy unscoped behaviour by defaulting to the local profile.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from . import profile_scope

logger = logging.getLogger("sensei.approval")


def _allowlist_path(profile: profile_scope.Profile | None = None) -> Path:
    return profile_scope.allowlist_dir(profile) / "allowed_commands.json"


def _load(profile: profile_scope.Profile | None = None) -> set[str]:
    path = _allowlist_path(profile)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read allowlist %s: %s", path, exc)
        return set()


def _save(allowed: set[str], profile: profile_scope.Profile | None = None) -> None:
    _allowlist_path(profile).write_text(
        json.dumps(sorted(allowed), indent=2), encoding="utf-8"
    )


def is_allowed(command: str, profile: profile_scope.Profile | None = None) -> bool:
    return command in _load(profile)


def allow(
    commands: Iterable[str], profile: profile_scope.Profile | None = None
) -> None:
    s = _load(profile)
    s.update(commands)
    _save(s, profile)


def revoke(
    commands: Iterable[str], profile: profile_scope.Profile | None = None
) -> None:
    s = _load(profile)
    s.difference_update(commands)
    _save(s, profile)
