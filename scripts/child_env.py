"""Build a child-process environment for spawning agent turns on behalf of a
*destination* profile, not the calling (sender) process.

Design idea (learned, not copied): a child that acts for a profile other than
the one whose tick/invocation spawned it must not inherit the launch
environment of the sender. Residue from the sender's shell/launcher (provider
credentials, scratch dir, profile policy vars) leaks across the boundary.
Instead, resolve the env against the DESTINATION home:

  1. Start from a minimal, delegated child env (not os.environ verbatim).
  2. Strip known launch-profile residue, resolved against the target home —
     a no-target strip resolves against the ambient override, which is the
     SENDER's home and silently wrong for cross-profile children.
  3. Scrub credential-looking vars the launcher's shell gave us by value
     pattern (no name-based strip can see those).
  4. Point TMPDIR at the destination's scratch dir.
  5. Overlay the destination's own secrets/env file, the same way a
     standalone `sensei -p <profile>` invocation would.

Return a refusal string on failure (target home unreadable, broken secrets
source) instead of raising — the fan-out delivery lane does not catch.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

# Env var substrings that mark launcher residue and must not reach a child
# acting for another profile.
_RESIDUE_SUBSTRINGS = (
    "SENSEI_MODEL",
    "SENSEI_HOME",
    "SENSEI_PROFILE",
    "SENSEI_ACTIVE_PROFILE",
    "SENSEI_TERMINAL_",
    "SENSEI_GATE_",
    "SENSEI_ALLOW_",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "HF_TOKEN",
)

_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),      # JWT-by-convention keys
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\."),     # JWTs
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), # GitHub tokens
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),    # Google keys
)


def _is_routed_home(home: Path) -> bool:
    """Ambient override actually points at `home` (i.e. the caller is already
    acting for this profile). Only then is a no-target strip valid."""
    ambient = os.environ.get("SENSEI_HOME", "")
    try:
        return bool(ambient) and Path(ambient).resolve() == home.resolve()
    except OSError:
        return False


def _strip_residue(env: Dict[str, str]) -> Dict[str, str]:
    return {
        k: v for k, v in env.items()
        if not any(s in k for s in _RESIDUE_SUBSTRINGS)
    }


def _scrub_secret_values(env: Dict[str, str]) -> Dict[str, str]:
    """Value-based scrub: credentials injected by systemd/Compose/the shell
    under innocuous names survive a name-based strip."""
    clean: Dict[str, str] = {}
    for k, v in env.items():
        if any(p.search(v) for p in _CREDENTIAL_VALUE_PATTERNS):
            continue
        clean[k] = v
    return clean


def _overlay_profile_secrets(
    env: Dict[str, str], home: Path
) -> Tuple[Dict[str, str], Optional[str]]:
    """Overlay the destination profile's own env file, like a standalone
    `sensei -p <profile>` run would."""
    env_file = home / ".env"
    if not env_file.exists():
        return env, None
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return env, f"cannot read profile env file {env_file}: {exc}"
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env, None


def destination_child_env(
    home: Path,
    base_env: Optional[Dict[str, str]] = None,
    inherit_credentials: bool = False,
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Return (env, refusal). Exactly one is None.

    `home` is the DELIVERY TARGET — do not re-resolve it from SENSEI_HOME or
    an active-profile changed after admission; the dispatcher owns it.
    """
    if not isinstance(home, Path):
        home = Path(home)
    if not home.exists():
        return None, f"delivery target no longer exists: {home}; do not resend"
    if not home.is_dir():
        return None, f"delivery target is not a directory: {home}; do not resend"
    try:
        home / ".probe-safe"
    except OSError:
        return None, f"delivery target unreadable (per-user 0700?): {home}; do not resend"

    # 1. Minimal delegated base — never os.environ verbatim when spawning
    #    cross-profile. The caller may pass a sanitized base.
    env = dict(base_env if base_env is not None else {"PATH": os.environ.get("PATH", os.defpath)})

    # 2. Strip launch residue resolved against the REAL target. A no-target
    #    strip resolves against the ambient override — the SENDER's home on
    #    cross-profile spawns — which is not routed here and would no-op.
    if not _is_routed_home(home):
        env = _strip_residue(env)
    else:
        env = _strip_residue(env)  # routed: still strip before overlay re-adds

    # 3. Sender's credential values never belong on a cross-profile child.
    if not inherit_credentials:
        env = _scrub_secret_values(env)

    # 4. Scratch space belongs to the destination.
    scratch = home / "tmp"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(scratch)
    except OSError:
        pass  # fall back to inherited TMPDIR only if overlay succeeds

    # 5. Destination's own secrets, as a standalone `-p <profile>` run gets.
    env, err = _overlay_profile_secrets(env, home)
    if err:
        return None, f"failed to build destination env for {home}: {err}"

    # Ownership marker for the child loop.
    env["SENSEI_HOME"] = str(home)
    return env, None


# Back-compat name used by delivery lanes.
served_profile_child_env = destination_child_env
