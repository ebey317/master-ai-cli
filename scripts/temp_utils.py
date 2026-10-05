"""Portable temporary directory utilities for Sensei.

Provides a single source of truth for scratch space that respects:
1. $TMPDIR / $TEMP / $TMP (standard env vars)
2. platform-default via tempfile.gettempdir()
3. optional Sensei-specific override via SENSEI_TMPDIR
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from typing import Optional


def get_sensei_tempdir() -> Path:
    """Return the preferred temporary directory for Sensei operations.

    Priority:
    1. $SENSEI_TMPDIR (explicit user override)
    2. $TMPDIR (POSIX standard)
    3. $TEMP / $TMP (Windows standard)
    4. tempfile.gettempdir() (platform default, e.g. /tmp, /var/tmp, C:\\Temp)
    """
    for var in ("SENSEI_TMPDIR", "TMPDIR", "TEMP", "TMP"):
        if val := os.environ.get(var):
            p = Path(val).expanduser().resolve()
            if p.exists() or p.parent.exists():  # parent exists → we can create
                return p
    return Path(tempfile.gettempdir()).resolve()


def make_sensei_tempdir(prefix: str = "sensei-", *, subdir: Optional[str] = None) -> Path:
    """Create a fresh temporary directory under the Sensei temp root.

    Args:
        prefix: Directory name prefix (default: "sensei-").
        subdir: Optional fixed subdirectory name (e.g. "scratch", "uploads").
                If given, creates/returns that fixed path instead of a unique one.

    Returns:
        Path to the created directory (guaranteed to exist).
    """
    root = get_sensei_tempdir()
    if subdir:
        target = root / subdir
        target.mkdir(parents=True, exist_ok=True)
        return target
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def sensei_temp_path(*parts: str, mkdir: bool = False) -> Path:
    """Resolve a path under the Sensei temp root, creating parents if requested."""
    p = get_sensei_tempdir().joinpath(*parts)
    if mkdir:
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


# Back-compat alias for gradual migration
get_tempdir = get_sensei_tempdir
