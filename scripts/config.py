"""Shared configuration helpers for Sensei's scripts.* package.

Single source of truth for well-known paths used by ported modules
(db_registry etc.), which previously carried an "assumes config helper
exists" comment. Keep this module import-cheap: stdlib only, no side
effects beyond defining constants, so any scripts.* module can pull a
helper inside a function without startup cost.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # yaml is an optional dep for this helper module
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# Sensei's home directory (~/.sensei), overridable for tests and
# multi-profile setups. skill_manager.py and skill_registry.py already
# honor SENSEI_SKILLS_DIR; this keeps one env var for the parent dir.
SENSEI_HOME = Path(os.environ.get("SENSEI_HOME", Path.home() / ".sensei"))


def get_state_db_path() -> Path:
    """Path of the shared sessions/state SQLite database.

    Consumers only hold connections; db_schema.ensure_schema builds the
    schema on first use, so the file never needs to pre-exist.
    """
    return SENSEI_HOME / "state.db"


# ---------------------------------------------------------------------------
# Config schema. Single source of truth so scripts.* modules (and the
# config validator) never import phantom packages like `sensei.config`.
# ---------------------------------------------------------------------------

TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on"})


def is_truthy_value(value, default: bool = False) -> bool:
    """Coerce bool-ish values using the shared truthy string set."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_STRINGS
    return bool(value)


def env_var_enabled(name: str, default: str = "") -> bool:
    """Return True when an environment variable is set to a truthy value."""
    return is_truthy_value(os.getenv(name, default), default=False)


DEFAULT_CONFIG: dict = {
    "plugins": {
        "enabled": [],  # list — never a quoted string
        "disabled": [],
    },
    "agent": {
        "disabled_toolsets": [],  # list, also accepts a bare scalar (string list)
    },
    "model_catalog": {
        "excluded_providers": {},  # mapping
    },
    "skills": {
        "disabled": [],
    },
}

CONFIG_YAML_PATH = SENSEI_HOME / "config.yaml"


def load_config() -> dict:
    """Load ~/.sensei/config.yaml, deep-merged over DEFAULT_CONFIG.

    Missing file = pure defaults (first-run safe). Malformed file raises
    (loud, per the same policy as upstream config validation).
    """
    import copy

    if yaml is None:  # yaml is heavy-optional; defaults-only fallback
        return copy.deepcopy(DEFAULT_CONFIG)

    if not CONFIG_YAML_PATH.exists():
        return copy.deepcopy(DEFAULT_CONFIG)

    user_cfg = yaml.safe_load(CONFIG_YAML_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(user_cfg, dict):
        raise ValueError(
            f"{CONFIG_YAML_PATH} must be a YAML mapping, got {type(user_cfg).__name__}"
        )

    merged = copy.deepcopy(DEFAULT_CONFIG)

    def deep_merge(dst: dict, src: dict) -> None:
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                deep_merge(dst[key], value)
            else:
                dst[key] = value

    deep_merge(merged, user_cfg)
    _known_paths = set()

    def collect_paths(node: dict, prefix: str = "") -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            _known_paths.add(path)
            if isinstance(value, dict):
                collect_paths(value, path)

    collect_paths(merged)
    del _known_paths
    return merged
