"""
Skill management tool for Sensei — create, edit, delete skills with safe locking.
Portable design: validate names before lock acquisition; lock files keyed on
sha256(basename) so length/chars never hit filesystem limits and no residue on failure.
"""

import hashlib
import json
import os
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Dict, Any


SKILLS_DIR = Path(os.environ.get("SENSEI_SKILLS_DIR", Path.home() / ".sensei" / "skills"))
LOCKS_DIR = SKILLS_DIR / ".locks"
LOCKS_DIR.mkdir(parents=True, exist_ok=True)


# ---- Name validation ---------------------------------------------------------

def _validate_name(name: str) -> Optional[str]:
    """
    Validate a skill name. Returns error message string if invalid, None if valid.
    Mirrors upstream: rejects empty, NUL, traversal, and over-long names.
    """
    if not name:
        return "skill name cannot be empty"
    if "\x00" in name:
        return "skill name cannot contain NUL bytes"
    if len(name) > 255:
        return "skill name too long (max 255 chars)"
    # Path traversal attempts
    parts = Path(name).parts
    if any(p in ("..", ".", "") for p in parts):
        return "skill name cannot contain path traversal (..) or empty segments"
    # Disallow absolute paths
    if Path(name).is_absolute():
        return "skill name cannot be an absolute path"
    return None


def _basename(name: str) -> str:
    """Extract basename from a potentially namespaced skill name (category/name)."""
    return Path(name).name


# ---- Digest-keyed locking ----------------------------------------------------

def _skill_lock_path(name: str) -> Path:
    """
    Return the lock file path for a skill, keyed on sha256(basename).
    Fixed-width filename (64 hex chars + .lock), never hits filesystem limits.
    `foo` and `category/foo` share the same lock.
    """
    base = _basename(name)
    digest = hashlib.sha256(base.encode()).hexdigest()
    return LOCKS_DIR / f"{digest}.lock"


@contextmanager
def _skill_lock(name: str):
    """
    Acquire an exclusive lock for the given skill name.
    Validation MUST be done before entering this context manager.
    """
    lock_path = _skill_lock_path(name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---- Skill filesystem helpers ------------------------------------------------

def _skill_dir(name: str) -> Path:
    """Resolve the skill directory path (skills/<name>)."""
    return SKILLS_DIR / name


def _skill_exists(name: str) -> bool:
    """Check if a skill directory exists."""
    return _skill_dir(name).is_dir()


def _find_skill(name: str) -> Optional[Path]:
    """
    Find a skill by name or basename. Returns the skill directory path or None.
    Used for read/update/delete actions where the user may provide just the basename.
    """
    # Direct match first
    direct = _skill_dir(name)
    if direct.is_dir():
        return direct
    # Fallback: search by basename (category/foo -> foo)
    base = _basename(name)
    for candidate in SKILLS_DIR.rglob(base):
        if candidate.is_dir() and candidate.name == base:
            # Ensure it's not in excluded dirs
            if not any(part.startswith(".") for part in candidate.relative_to(SKILLS_DIR).parts):
                return candidate
    return None


# ---- Public API --------------------------------------------------------------

def skill_manage(action: str, name: str, content: Optional[str] = None) -> str:
    """
    Manage skills: create, read, update, delete.
    Returns JSON string with {success: bool, error?: str, data?: any}.
    """
    # Validation runs BEFORE any lock acquisition
    if action == "create":
        err = _validate_name(name)
        if err:
            return json.dumps({"success": False, "error": err})
    else:
        # For non-create actions, validate the basename (what the lock keys on)
        err = _validate_name(_basename(name))
        if err:
            return json.dumps({"success": False, "error": err})

    # Acquire lock AFTER validation
    with _skill_lock(name):
        return _skill_manage_locked(action, name, content)


def _skill_manage_locked(action: str, name: str, content: Optional[str]) -> str:
    """Internal implementation assuming lock is already held."""
    skill_path = _skill_dir(name)

    if action == "create":
        if skill_path.exists():
            return json.dumps({"success": False, "error": f"skill '{name}' already exists"})
        skill_path.mkdir(parents=True, exist_ok=True)
        (skill_path / "skill.md").write_text(content or "")
        return json.dumps({"success": True, "data": {"path": str(skill_path)}})

    # For read/update/delete, resolve via _find_skill to support basename lookup
    resolved = _find_skill(name)
    if not resolved:
        return json.dumps({"success": False, "error": f"skill '{name}' not found"})

    if action == "read":
        skill_md = resolved / "skill.md"
        if not skill_md.exists():
            return json.dumps({"success": False, "error": f"skill '{name}' has no skill.md"})
        return json.dumps({"success": True, "data": {"content": skill_md.read_text(), "path": str(resolved)}})

    if action == "update":
        if content is None:
            return json.dumps({"success": False, "error": "content required for update"})
        (resolved / "skill.md").write_text(content)
        return json.dumps({"success": True, "data": {"path": str(resolved)}})

    if action == "delete":
        import shutil
        shutil.rmtree(resolved)
        # Clean up empty parent directories
        parent = resolved.parent
        while parent != SKILLS_DIR and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        return json.dumps({"success": True, "data": {"deleted": str(resolved)}})

    return json.dumps({"success": False, "error": f"unknown action: {action}"})


# ---- Exclusion sets for other subsystems ------------------------------------

EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".curator_backups", ".locks",
    ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
))

SCAN_SKIP_PARTS = {'.git', '.github', '.hub', '.archive', '.locks'}
NON_PACKAGE_TOPS = {".curator_backups", ".hub", ".archive", ".locks"}
SKIP_PARTS = {".archive", ".hub", ".locks", "node_modules", ".git"}
