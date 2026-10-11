#!/usr/bin/env python3
"""
Plugin catalog loader for Sensei.

Loads plugin_catalog.yaml, validates entries, and provides a query API
for the router to discover routable components (skills, tools, sub-agents).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml


CATALOG_PATH = Path(__file__).parent.parent / "plugin_catalog.yaml"


class PluginType(str, Enum):
    SKILL = "skill"
    TOOL = "tool"
    SUB_AGENT = "sub_agent"


class PluginTier(str, Enum):
    CORE = "core"
    COMMUNITY = "community"
    EXPERIMENTAL = "experimental"


@dataclass
class PluginMetadata:
    complexity: str = "unknown"
    typical_latency_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PluginEntry:
    name: str
    type: PluginType
    description: str
    entry_point: str
    capabilities: list[str]
    requires_env: list[str]
    tier: PluginTier
    metadata: PluginMetadata = field(default_factory=PluginMetadata)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginEntry":
        meta = data.get("metadata", {})
        return cls(
            name=data["name"],
            type=PluginType(data["type"]),
            description=data["description"],
            entry_point=data["entry_point"],
            capabilities=data.get("capabilities", []),
            requires_env=data.get("requires_env", []),
            tier=PluginTier(data.get("tier", "community")),
            metadata=PluginMetadata(
                complexity=meta.get("complexity", "unknown"),
                typical_latency_ms=meta.get("typical_latency_ms", 0),
                extra={k: v for k, v in meta.items() if k not in ("complexity", "typical_latency_ms")},
            ),
        )

    def env_satisfied(self) -> tuple[bool, list[str]]:
        missing = [var for var in self.requires_env if not os.getenv(var)]
        return len(missing) == 0, missing

    def load(self) -> Any:
        """Dynamically import and return the plugin class/function."""
        module_path, attr = self.entry_point.split(":", 1)
        module = __import__(module_path, fromlist=[attr])
        return getattr(module, attr)


class PluginCatalog:
    def __init__(self, path: Path = CATALOG_PATH):
        self.path = path
        self._plugins: list[PluginEntry] = []
        self._by_name: dict[str, PluginEntry] = {}
        self._by_capability: dict[str, list[PluginEntry]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"Plugin catalog not found: {self.path}")
        with self.path.open() as f:
            data = yaml.safe_load(f)
        if data.get("schema_version") != 1:
            raise ValueError(f"Unsupported catalog schema version: {data.get('schema_version')}")
        for entry_data in data.get("plugins", []):
            entry = PluginEntry.from_dict(entry_data)
            self._plugins.append(entry)
            self._by_name[entry.name] = entry
            for cap in entry.capabilities:
                self._by_capability.setdefault(cap, []).append(entry)

    def get(self, name: str) -> Optional[PluginEntry]:
        return self._by_name.get(name)

    def find_by_capability(self, capability: str, tier: Optional[PluginTier] = None) -> list[PluginEntry]:
        results = self._by_capability.get(capability, [])
        if tier:
            results = [p for p in results if p.tier == tier]
        return results

    def find_by_type(self, plugin_type: PluginType, tier: Optional[PluginTier] = None) -> list[PluginEntry]:
        results = [p for p in self._plugins if p.type == plugin_type]
        if tier:
            results = [p for p in results if p.tier == tier]
        return results

    def all(self, tier: Optional[PluginTier] = None) -> list[PluginEntry]:
        if tier:
            return [p for p in self._plugins if p.tier == tier]
        return list(self._plugins)

    def available(self, tier: Optional[PluginTier] = None) -> list[PluginEntry]:
        """Return plugins whose required env vars are satisfied."""
        result = []
        for p in self.all(tier):
            ok, _ = p.env_satisfied()
            if ok:
                result.append(p)
        return result


def main() -> int:
    """CLI for inspecting the plugin catalog."""
    import argparse

    parser = argparse.ArgumentParser(description="Sensei plugin catalog")
    parser.add_argument("--list", action="store_true", help="List all plugins")
    parser.add_argument("--type", choices=[t.value for t in PluginType], help="Filter by type")
    parser.add_argument("--tier", choices=[t.value for t in PluginTier], help="Filter by tier")
    parser.add_argument("--capability", help="Filter by capability")
    parser.add_argument("--available", action="store_true", help="Only show plugins with satisfied env")
    parser.add_argument("--get", help="Show details for a specific plugin")
    args = parser.parse_args()

    catalog = PluginCatalog()

    if args.get:
        plugin = catalog.get(args.get)
        if not plugin:
            print(f"Plugin not found: {args.get}", file=sys.stderr)
            return 1
        print(f"Name:        {plugin.name}")
        print(f"Type:        {plugin.type.value}")
        print(f"Tier:        {plugin.tier.value}")
        print(f"Description: {plugin.description}")
        print(f"Entry point: {plugin.entry_point}")
        print(f"Capabilities: {', '.join(plugin.capabilities) or 'none'}")
        print(f"Requires env: {', '.join(plugin.requires_env) or 'none'}")
        print(f"Complexity:   {plugin.metadata.complexity}")
        print(f"Latency:     {plugin.metadata.typical_latency_ms}ms")
        ok, missing = plugin.env_satisfied()
        print(f"Env OK:      {'yes' if ok else 'no'}" + (f" (missing: {', '.join(missing)})" if missing else ""))
        return 0

    plugins = catalog.all()
    if args.type:
        plugins = [p for p in plugins if p.type.value == args.type]
    if args.tier:
        plugins = [p for p in plugins if p.tier.value == args.tier]
    if args.capability:
        plugins = [p for p in plugins if args.capability in p.capabilities]
    if args.available:
        plugins = [p for p in plugins if p.env_satisfied()[0]]

    for p in plugins:
        ok, _ = p.env_satisfied()
        status = "✓" if ok else "✗"
        print(f"{status} [{p.tier.value}] {p.name} ({p.type.value}) - {p.description}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
