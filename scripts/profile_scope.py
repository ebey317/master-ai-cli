"""Route-aware profile scope for Sensei.

The upstream idea is that a multiplexed gateway must not let a routed profile's
turn inherit the LAUNCH profile's cwd, env, config, approvals or caches. This
module is the minimal, local-first equivalent: a context-var based profile scope
that tools read instead of ``os.environ`` / ``os.getcwd()`` / module globals.
"""
from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class Profile:
    name: str
    home_dir: Optional[Path] = None
    cwd: Optional[Path] = None
    env: Mapping[str, str] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)
    sandbox_default: str = "/tmp/sensei-sandbox"

    @property
    def resolved_home(self) -> Path:
        return Path(self.home_dir or os.path.expanduser("~")).expanduser().resolve()

    @property
    def resolved_cwd(self) -> Path:
        if self.cwd:
            p = Path(self.cwd).expanduser()
            if p.is_absolute():
                return p.resolve()
            return (self.resolved_home / p).resolve()
        # Mirror the upstream import rule: local -> home, sandbox otherwise.
        if self.name == "local":
            return self.resolved_home
        return Path(self.sandbox_default).resolve()


def _default_profile() -> Profile:
    return Profile(name="local", home_dir=Path.home())


_ACTIVE: ContextVar[Profile] = ContextVar(
    "sensei_active_profile", default=_default_profile()
)


def current() -> Profile:
    return _ACTIVE.get()


@contextlib.contextmanager
def with_profile(profile: Profile):
    token = _ACTIVE.set(profile)
    try:
        yield profile
    finally:
        _ACTIVE.reset(token)


def reset() -> None:
    _ACTIVE.set(_default_profile())


def profile_key(profile: Optional[Profile] = None) -> str:
    p = profile or current()
    return f"{p.name}@{p.resolved_home}"


def cache_key(*parts: Any, profile: Optional[Profile] = None) -> tuple:
    return (profile_key(profile), *parts)


def scoped_env(profile: Optional[Profile] = None) -> dict[str, str]:
    p = profile or current()
    merged = dict(os.environ)
    merged.update(p.env)
    merged["SENSEI_PROFILE"] = p.name
    merged["SENSEI_HOME"] = str(p.resolved_home)
    merged["SENSEI_CWD"] = str(p.resolved_cwd)
    return merged


def get_terminal_env() -> dict[str, str]:
    return scoped_env()


def get_terminal_cwd() -> Path:
    return current().resolved_cwd


def allowlist_dir(profile: Optional[Profile] = None) -> Path:
    p = profile or current()
    base = p.resolved_home / ".sensei"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_value(
    *keys: str, default: Any = None, profile: Optional[Profile] = None
) -> Any:
    node = (profile or current()).config
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node
