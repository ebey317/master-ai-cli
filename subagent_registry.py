"""Subagent registry (P1.5).

Drop a Python file into ~/scripts/subagents/ — it auto-registers at
import time via :func:`discover`. Each subagent module exports:

  name        : str  — short id (snake_case)
  description : str  — one-line summary
  run(task, context=None) -> dict   — returns inert structured data;
                                       never RUN: / EDIT: / CREATE:
                                       directives. The executor will
                                       NOT parse subagent output for
                                       directives.

Public API:
    discover() -> int                       — scan + register; returns count
    list_subagents() -> list[Subagent]
    get(name) -> Optional[Subagent]
    run(name, task, context=None) -> dict   — wrapper around subagent.run
    SUBAGENTS_DIR                           — default discovery dir

Discovery is idempotent: discover() can be called multiple times and
won't duplicate registrations. Import errors in subagent files are
swallowed (printed to stderr) so a broken subagent doesn't bring the
whole registry down.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


SUBAGENTS_DIR = Path.home() / "scripts" / "subagents"


@dataclass
class Subagent:
    name: str
    description: str
    run: Callable[..., dict]
    source: str = ""           # absolute path to source file
    module_name: str = ""      # for re-discovery


_REGISTRY: dict[str, Subagent] = {}
_DISCOVERED_DIR: Optional[Path] = None


def _load_module_from_path(path: Path):
    mod_name = f"subagent_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _register_module(mod, source_path: str) -> bool:
    name = getattr(mod, "name", None)
    description = getattr(mod, "description", None)
    run_fn = getattr(mod, "run", None)
    if not name or not isinstance(name, str):
        return False
    if not callable(run_fn):
        return False
    sa = Subagent(
        name=name,
        description=(description or "").strip() or "(no description)",
        run=run_fn,
        source=source_path,
        module_name=getattr(mod, "__name__", ""),
    )
    _REGISTRY[name] = sa  # idempotent overwrite
    return True


def discover(directory: Optional[Path] = None) -> int:
    """Scan ``directory`` (defaults to SUBAGENTS_DIR) for *.py files and
    register each one that exposes the contract (name, description, run).
    Returns the number of subagents currently registered (cumulative)."""
    global _DISCOVERED_DIR
    d = directory or SUBAGENTS_DIR
    _DISCOVERED_DIR = Path(d)
    if not _DISCOVERED_DIR.is_dir():
        # Fallback: if the default ~/scripts/subagents doesn't exist, try the
        # repo-local subagents/ directory (useful for git clones and editable installs).
        fallback = Path(__file__).parent / "subagents"
        if fallback.is_dir():
            _DISCOVERED_DIR = fallback
        else:
            return len(_REGISTRY)
    for fp in sorted(_DISCOVERED_DIR.glob("*.py")):
        if fp.name.startswith("_"):
            continue
        try:
            mod = _load_module_from_path(fp)
            if mod is None:
                continue
            _register_module(mod, str(fp))
        except Exception:
            # Print to stderr but never crash the registry — a broken
            # subagent file shouldn't take down Sensei.
            sys.stderr.write(f"subagent load failed: {fp}\n")
            traceback.print_exc(file=sys.stderr)
            continue
    return len(_REGISTRY)


def list_subagents() -> list[Subagent]:
    return sorted(_REGISTRY.values(), key=lambda s: s.name)


def get(name: str) -> Optional[Subagent]:
    if not isinstance(name, str):
        return None
    return _REGISTRY.get(name)


def run(name: str, task: str = "", context: Optional[dict] = None) -> dict:
    """Execute the named subagent. Returns {'error': ...} on lookup or
    runtime failure; the subagent's own return dict otherwise. The
    return value is treated as INERT data by the executor — no directive
    parsing happens on it."""
    sa = get(name)
    if sa is None:
        return {"error": f"unknown subagent: {name!r}",
                "available": [s.name for s in list_subagents()]}
    try:
        result = sa.run(task, context=context)
        if not isinstance(result, dict):
            # Normalize: wrap non-dict returns so callers always get a
            # predictable shape.
            return {"result": result, "subagent": name}
        # Always tag the result with the subagent that produced it.
        if "subagent" not in result:
            result["subagent"] = name
        return result
    except Exception as e:
        return {"error": str(e), "subagent": name, "exception": type(e).__name__}


def clear() -> None:
    """Drop all registered subagents. Mainly for tests."""
    _REGISTRY.clear()


# Auto-discover at import time so callers don't have to remember.
discover()
