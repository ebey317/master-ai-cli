"""Project-root-keyed skill registry for multi-repo session isolation.

Portable reimplementation of the upstream skill_commands design: the cached
skill registry is tagged by (platform, home, project_root) so that concurrent
sessions in different repositories each see their own project-local skills
without cross-contamination or stale cache hits.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .session_cwd import get_session_cwd, resolve_effective_cwd

_SKILL_COMMANDS: Dict[str, Dict[str, Any]] = {}
_SKILL_COMMANDS_PLATFORM: Optional[str] = None
_SKILL_COMMANDS_HOME: Optional[str] = None
_SKILL_COMMANDS_PROJECT: Optional[str] = None
_PUBLISH_LOCK = threading.Lock()

def _resolve_platform_tag() -> str:
    """Platform identifier (e.g., 'tui', 'api', 'test'). Override in embedding."""
    import platform
    return platform.system().lower()

def _resolve_home_tag() -> str:
    """Active profile/home directory tag. Override in embedding for multi-profile."""
    return str(Path.home())

def _resolve_project_tag() -> Optional[str]:
    """Project root for the current session (None if not in a git repo)."""
    cwd = resolve_effective_cwd()
    try:
        # Find git root
        for parent in [cwd] + list(cwd.parents):
            if (parent / ".git").exists():
                return str(parent.resolve())
    except Exception:
        pass
    return None

def _current_cache_key() -> Tuple[str, str, Optional[str]]:
    """Composite key for cache invalidation."""
    return (_resolve_platform_tag(), _resolve_home_tag(), _resolve_project_tag())

def scan_skills() -> Dict[str, Dict[str, Any]]:
    """Discover skills from all sources and publish atomically.

    Scans: built-in, user home (~/.sensei/skills), project (.sensei/skills).
    Project skills are only included when a project root is detected.
    """
    global _SKILL_COMMANDS, _SKILL_COMMANDS_PLATFORM, _SKILL_COMMANDS_HOME, _SKILL_COMMANDS_PROJECT

    platform = _resolve_platform_tag()
    home = _resolve_home_tag()
    project = _resolve_project_tag()

    # Build into a local map to avoid partial publication races
    commands: Dict[str, Dict[str, Any]] = {}
    seen_names: set[str] = set()

    # 1. Built-in skills (placeholder — embedder provides)
    # embedder can monkey-patch or subclass to inject builtins

    # 2. Home skills
    home_skills_dir = Path(home) / ".sensei" / "skills"
    if home_skills_dir.exists():
        for skill_dir in home_skills_dir.iterdir():
            if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                _try_load_skill(skill_dir, commands, seen_names, "home")

    # 3. Project skills (only if inside a repo)
    if project:
        project_skills_dir = Path(project) / ".sensei" / "skills"
        if project_skills_dir.exists():
            for skill_dir in project_skills_dir.iterdir():
                if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                    _try_load_skill(skill_dir, commands, seen_names, "project")

    # Atomic publish
    with _PUBLISH_LOCK:
        _SKILL_COMMANDS = commands
        _SKILL_COMMANDS_PLATFORM = platform
        _SKILL_COMMANDS_HOME = home
        _SKILL_COMMANDS_PROJECT = project

    return commands

def _try_load_skill(
    skill_dir: Path,
    commands: Dict[str, Dict[str, Any]],
    seen_names: set[str],
    source: str,
) -> None:
    """Attempt to load a skill directory; silently skip on failure."""
    try:
        # Expect skill.json or skill.yaml with at least {"name": "...", "entry": "..."}
        import json
        manifest_path = skill_dir / "skill.json"
        if not manifest_path.exists():
            manifest_path = skill_dir / "skill.yaml"
        if not manifest_path.exists():
            return

        if manifest_path.suffix == ".json":
            manifest = json.loads(manifest_path.read_text())
        else:
            import yaml
            manifest = yaml.safe_load(manifest_path.read_text())

        name = manifest.get("name") or skill_dir.name
        slug = f"/{name}"
        if slug in seen_names:
            return  # first wins
        seen_names.add(slug)

        commands[slug] = {
            "name": name,
            "description": manifest.get("description", ""),
            "entry": manifest.get("entry", ""),
            "source": source,
            "path": str(skill_dir),
            "manifest": manifest,
        }
    except Exception:
        pass  # invalid skill — ignore

def get_skills() -> Dict[str, Dict[str, Any]]:
    """Return current skill map; rescan if platform/home/project context changed."""
    current_key = _current_cache_key()
    with _PUBLISH_LOCK:
        commands = _SKILL_COMMANDS
        cached_key = (_SKILL_COMMANDS_PLATFORM, _SKILL_COMMANDS_HOME, _SKILL_COMMANDS_PROJECT)
        is_fresh = bool(commands) and cached_key == current_key

    if is_fresh:
        return commands
    return scan_skills()

def invalidate_cache() -> None:
    """Force rescan on next get_skills() call (e.g., after profile switch)."""
    with _PUBLISH_LOCK:
        global _SKILL_COMMANDS, _SKILL_COMMANDS_PLATFORM, _SKILL_COMMANDS_HOME, _SKILL_COMMANDS_PROJECT
        _SKILL_COMMANDS = {}
        _SKILL_COMMANDS_PLATFORM = None
        _SKILL_COMMANDS_HOME = None
        _SKILL_COMMANDS_PROJECT = None
