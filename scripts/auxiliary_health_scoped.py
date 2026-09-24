"""Unhealthy-provider marks keyed by the active profile.

A 402 / rate-limit on profile A's billing account must not hide the provider
from profile B's differently-funded account in the same process.
"""
from __future__ import annotations

import time
from typing import Optional

from . import profile_scope

_UNHEALTHY: dict[tuple, float] = {}


def _key(provider: str, base_url: Optional[str]) -> tuple:
    return profile_scope.cache_key("unhealthy", provider, base_url or "*")


def mark_unhealthy(
    provider: str, base_url: Optional[str] = None, ttl_seconds: float = 300
) -> None:
    _UNHEALTHY[_key(provider, base_url)] = time.monotonic() + ttl_seconds


def is_unhealthy(provider: str, base_url: Optional[str] = None) -> bool:
    expires = _UNHEALTHY.get(_key(provider, base_url))
    if expires is None:
        return False
    if time.monotonic() > expires:
        _UNHEALTHY.pop(_key(provider, base_url), None)
        return False
    return True
