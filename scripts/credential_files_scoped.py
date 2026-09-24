"""Credential file mounts scoped to the active profile.

Mounts like ``~/.aws/credentials`` must resolve relative to the routed profile's
home, not the user who launched the agent process.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from . import profile_scope


def resolve_mounts(
    spec: Mapping[str, str | Path],
    profile: Optional[profile_scope.Profile] = None,
) -> dict[str, Path]:
    home = (profile or profile_scope.current()).resolved_home
    out: dict[str, Path] = {}
    for container, host in spec.items():
        p = Path(host).expanduser()
        if not p.is_absolute():
            p = (home / p).resolve()
        out[container] = p
    return out


def cache_key(*parts: object, profile: Optional[profile_scope.Profile] = None) -> tuple:
    return profile_scope.cache_key("credential_files", *parts, profile=profile)
