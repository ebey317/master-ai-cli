"""Schema-aware configuration validator for Sensei.

Detects quoted container values (e.g., enabled: '["a","b"]') in slots that
expect real YAML lists/mappings. Such values are ignored by isinstance-gated
readers while config get echoes them back, causing silent failures.
"""
from __future__ import annotations

import shlex
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sensei.config import DEFAULT_CONFIG, load_config  # type: ignore[attr-defined]


# Keys that are deliberately absent from DEFAULT_CONFIG but must be containers.
# Add any Sensei-specific keys here that are container-typed but not in defaults.
_KNOWN_CONTAINER_TYPES: Dict[str, str] = {
    # Example Sensei keys (adjust to actual schema):
    # "plugins.enabled": "list",
    # "plugins.disabled": "list",
    # "model_catalog.excluded_providers": "list",
}

# List slots whose readers accept a bare scalar as a single-item list via
# parse_config_string_list. These are NOT flagged when quoted.
_SCALAR_AS_ONE_ITEM_LIST_KEYS = frozenset({
    # "agent.disabled_toolsets",
    # "skills.disabled",
})


def _looks_structured_value(value: str) -> bool:
    """Heuristic: does the string look like a YAML list/dict literal?"""
    stripped = value.strip()
    return (
        (stripped.startswith("[") and stripped.endswith("]")) or
        (stripped.startswith("{") and stripped.endswith("}"))
    )


def _container_slots() -> Dict[str, str]:
    """Return dotted key -> "list"/"mapping" for every slot the schema fixes to a container.

    Walks DEFAULT_CONFIG (sections included) plus _KNOWN_CONTAINER_TYPES for roots it omits.
    """
    slots: Dict[str, str] = {}

    def walk(node: Dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                slots[path] = "mapping"
                walk(value, path)
            elif isinstance(value, list):
                slots[path] = "list"

    walk(DEFAULT_CONFIG, "")
    slots.update(_KNOWN_CONTAINER_TYPES)
    return slots


def validate_quoted_containers(config: Dict[str, Any]) -> List[Dict[str, str]]:
    """Validate config for quoted container values.

    Returns a list of warning dicts with keys: key, kind, quoted_value, remedy.
    """
    warnings: List[Dict[str, str]] = []

    for key, kind in _container_slots().items():
        if key in _SCALAR_AS_ONE_ITEM_LIST_KEYS:
            continue

        # Navigate to the value using dotted key
        value = config
        try:
            for part in key.split("."):
                value = value[part]
        except (KeyError, TypeError):
            continue

        if not isinstance(value, str) or not _looks_structured_value(value):
            continue

        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError:
            continue

        if isinstance(parsed, (list, dict)):
            remedy = f"Run: sensei config set {key} {shlex.quote(value)}  (stores a real {kind}), or remove the quotes in config.yaml"
            warnings.append({
                "key": key,
                "kind": kind,
                "quoted_value": value,
                "remedy": remedy,
            })

    return warnings


def validate_config_structure(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Validate config.yaml structure and return detected issues.

    Catches common YAML mistakes that otherwise surface as confusing runtime errors.
    """
    if config is None:
        config = load_config()

    issues: List[Dict[str, str]] = []

    # Existing validation checks would go here...

    # Quoted container detection
    issues.extend(validate_quoted_containers(config))

    return issues


def print_config_warnings(config: Optional[Dict[str, Any]] = None) -> None:
    """Print config warnings to stderr (for startup banner)."""
    import sys
    issues = validate_config_structure(config)
    for issue in issues:
        print(f"⚠ Config: {issue['key']} is the quoted string {issue['quoted_value']!r} — "
              f"Sensei expects a YAML {issue['kind']} here and every reader ignores the string",
              file=sys.stderr)
        print(f"   Remedy: {issue['remedy']}", file=sys.stderr)


if __name__ == "__main__":
    # CLI entry for `sensei doctor --config` or standalone testing
    import json
    issues = validate_config_structure()
    print(json.dumps(issues, indent=2))
