"""Route-aware secret redaction toggle.

``redaction_enabled()`` resolves the switch for the active profile:
scoped env -> scoped config -> launch-time snapshot. This prevents profile A's
turn from redacting (or failing to redact) based on the launch profile.
"""
from __future__ import annotations

import os
from typing import Optional

from . import profile_scope

_REDACT_ENABLED_AT_IMPORT = os.getenv("SENSEI_REDACT_SECRETS", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def redaction_enabled(profile: Optional[profile_scope.Profile] = None) -> bool:
    p = profile or profile_scope.current()

    # Fast path for legacy single-profile local runs with no scoped config.
    if p.name == "local" and not p.config and not p.env:
        return _REDACT_ENABLED_AT_IMPORT

    env_flag = p.env.get("SENSEI_REDACT_SECRETS")
    if env_flag is not None:
        return env_flag.lower() in {"1", "true", "yes", "on"}

    cfg = profile_scope.config_value(
        "security", "redact_secrets", default=None, profile=p
    )
    if cfg is not None:
        return bool(cfg)

    return _REDACT_ENABLED_AT_IMPORT
