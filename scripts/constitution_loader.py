"""
Load the user's constitution/identity file (SENSEI.md in ~/.config/sensei/).

This is the Sensei equivalent of Hermes' SOUL.md — a user-authored file that
defines the agent's identity, principles, and operating constitution. It sits
in the same trust class as config.yaml: agent writes go through approval gates,
no repository checkout can plant it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .context_scanner import (
    ContextFileEntry,
    ContextFileStatus,
    ScanResult,
    estimate_tokens_rough,
    load_context_file,
    scan_context_content,
)

logger = logging.getLogger(__name__)

# Default location for user's constitution file
DEFAULT_CONSTITUTION_PATH = Path.home() / ".config" / "sensei" / "SENSEI.md"
ENV_CONSTITUTION_PATH = "SENSEI_CONSTITUTION_PATH"


def get_constitution_path() -> Path:
    """Resolve the constitution file path (env var overrides default)."""
    env_path = os.environ.get(ENV_CONSTITUTION_PATH)
    if env_path:
        return Path(env_path).expanduser().resolve()
    return DEFAULT_CONSTITUTION_PATH


def load_constitution(
    max_chars: int | None = None,
) -> tuple[str, ScanResult, ContextFileEntry]:
    """
    Load the user's constitution file with injection scanning.

    Returns:
        (content, scan_result, manifest_entry)
        - content: The loaded content (or blocked marker if blocked — but for
          user-authored files we use FLAGGED so content is always the original).
        - scan_result: Full scan details including findings.
        - manifest_entry: Entry for /context manifest (status = loaded/flagged).
    """
    path = get_constitution_path()

    raw_content, load_status = load_context_file(path, max_chars)

    if load_status != ContextFileStatus.LOADED:
        # Empty, unreadable, or truncated before scan
        scan_result = ScanResult(
            action=ScanAction.LOAD if load_status == ContextFileStatus.TRUNCATED else ScanAction.BLOCKED,
            findings=tuple(),
            content=raw_content,
            status_label=load_status.value,
        )
        entry = ContextFileEntry(
            label="SENSEI.md",
            path=str(path),
            chars=len(raw_content),
            est_tokens=estimate_tokens_rough(raw_content),
            status=load_status,
        )
        return raw_content, scan_result, entry

    # Scan with user_authored=True — this is the key difference from project files
    scan_result = scan_context_content(raw_content, "SENSEI.md", user_authored=True)

    # Determine manifest status from scan result
    if scan_result.action == ScanAction.FLAGGED:
        manifest_status = ContextFileStatus.FLAGGED
    elif scan_result.action == ScanAction.BLOCKED:
        # Should not happen with user_authored=True, but defend anyway
        manifest_status = ContextFileStatus.BLOCKED
    elif load_status == ContextFileStatus.TRUNCATED:
        manifest_status = ContextFileStatus.TRUNCATED
    else:
        manifest_status = ContextFileStatus.LOADED

    entry = ContextFileEntry(
        label="SENSEI.md",
        path=str(path),
        chars=len(raw_content),
        est_tokens=estimate_tokens_rough(raw_content),
        status=manifest_status,
    )

    return scan_result.content, scan_result, entry


def list_constitution_sources(
    max_chars: int | None = None,
) -> list[ContextFileEntry]:
    """
    List constitution file source for /context manifest (read-only, no prompt build).

    Mirrors the same scan logic as load_constitution but does not build prompts.
    """
    path = get_constitution_path()
    raw_content, load_status = load_context_file(path, max_chars)

    if load_status != ContextFileStatus.LOADED:
        return [ContextFileEntry(
            label="SENSEI.md",
            path=str(path),
            chars=len(raw_content),
            est_tokens=estimate_tokens_rough(raw_content),
            status=load_status,
        )]

    scan_result = scan_context_content(raw_content, "SENSEI.md", user_authored=True)

    if scan_result.action == ScanAction.FLAGGED:
        manifest_status = ContextFileStatus.FLAGGED
    elif scan_result.action == ScanAction.BLOCKED:
        manifest_status = ContextFileStatus.BLOCKED
    elif load_status == ContextFileStatus.TRUNCATED:
        manifest_status = ContextFileStatus.TRUNCATED
    else:
        manifest_status = ContextFileStatus.LOADED

    return [ContextFileEntry(
        label="SENSEI.md",
        path=str(path),
        chars=len(raw_content),
        est_tokens=estimate_tokens_rough(raw_content),
        status=manifest_status,
    )]
