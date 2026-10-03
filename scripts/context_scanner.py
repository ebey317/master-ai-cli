"""
Context file injection scanner with trust-level awareness.

User-authored files (constitution, identity) live in the same trust class as config:
agent writes go through approval gates, no repo checkout can plant them.
Project-sourced files (AGENTS.md, .cursorrules, etc.) arrive with a clone and
keep strict blocking behavior.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# Canonical injection patterns — reported-speech form cannot be separated
# from real attacks by regex ("I want you to ignore all previous instructions"
# is a canonical payload).
THREAT_PATTERNS: Final[list[re.Pattern]] = [
    re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions?", re.I),
    re.compile(r"disregard\s+(?:all\s+)?(?:prior|previous)\s+instructions?", re.I),
    re.compile(r"forget\s+(?:all\s+)?(?:prior|previous)\s+instructions?", re.I),
    re.compile(r"override\s+(?:all\s+)?(?:prior|previous)\s+instructions?", re.I),
    re.compile(r"system\s+prompt\s*(?:is|:)\s*", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+\w+", re.I),
    re.compile(r"act\s+as\s+(?:a|an)\s+\w+", re.I),
    re.compile(r"pretend\s+to\s+be\s+(?:a|an)\s+\w+", re.I),
    re.compile(r"new\s+instructions?:", re.I),
    re.compile(r"updated\s+instructions?:", re.I),
    re.compile(r"<%\s*INJECT\s+%>", re.I),
    re.compile(r"\[INST\]", re.I),
    re.compile(r"<<SYS>>", re.I),
]


class ScanAction(Enum):
    """What to do with scanned content."""
    LOAD = "load"           # Clean, load normally
    FLAGGED = "flagged"     # User-authored, matched patterns → load + warn
    BLOCKED = "blocked"     # Project file, matched patterns → block


@dataclass(frozen=True)
class ScanResult:
    """Result of scanning a context file."""
    action: ScanAction
    findings: tuple[str, ...]
    content: str  # Original content (for LOAD/FLAGGED) or blocked marker (for BLOCKED)
    status_label: str  # For manifest: "loaded", "flagged", "blocked"

    @property
    def loaded(self) -> bool:
        return self.action in (ScanAction.LOAD, ScanAction.FLAGGED)


def _scan_for_threats(text: str) -> list[str]:
    """Return list of matched pattern descriptions."""
    findings = []
    for pattern in THREAT_PATTERNS:
        for match in pattern.finditer(text):
            # Extract a readable snippet around the match
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 20)
            snippet = text[start:end].replace("\n", " ")
            findings.append(f"{pattern.pattern!r} matched near: …{snippet}…")
    return findings


def scan_context_content(
    content: str,
    filename: str,
    *,
    user_authored: bool = False,
) -> ScanResult:
    """
    Scan a context file for prompt injection.

    Args:
        content: Raw file content.
        filename: Source filename (for logging/manifest).
        user_authored: True for user's own constitution/identity file (e.g., SOUL.md,
            SENSEI.md in user config dir). False for project-sourced files
            (AGENTS.md, .cursorrules, .sensei.md in repo).

    Returns:
        ScanResult with action, findings, and content (original or blocked marker).
    """
    # Strip UTF-8 BOM (Windows editor artifact, not injection)
    if content.startswith("\ufeff"):
        content = content[1:]

    findings = _scan_for_threats(content)

    if not findings:
        return ScanResult(
            action=ScanAction.LOAD,
            findings=tuple(),
            content=content,
            status_label="loaded",
        )

    if user_authored:
        # User's own file: warn but load. Same trust class as config.yaml.
        logger.warning(
            "User-authored context file %s flagged: %s",
            filename,
            ", ".join(findings),
        )
        return ScanResult(
            action=ScanAction.FLAGGED,
            findings=tuple(findings),
            content=content,
            status_label="flagged",
        )

    # Project-sourced file: block entirely.
    logger.warning(
        "Project context file %s blocked: %s",
        filename,
        ", ".join(findings),
    )
    blocked_marker = (
        f"[BLOCKED: {filename} contained potential prompt injection "
        f"({', '.join(findings)}). Content not loaded.]"
    )
    return ScanResult(
        action=ScanAction.BLOCKED,
        findings=tuple(findings),
        content=blocked_marker,
        status_label="blocked",
    )


class ContextFileStatus(Enum):
    """Manifest status for /context listing."""
    LOADED = "loaded"
    TRUNCATED = "truncated"
    FLAGGED = "flagged"
    SHADOWED = "shadowed"
    BLOCKED = "blocked"
    EMPTY = "empty"
    UNREADABLE = "unreadable"
    SUPPRESSED = "suppressed"

    @property
    def icon(self) -> str:
        return {
            ContextFileStatus.LOADED: "●",
            ContextFileStatus.TRUNCATED: "◐",
            ContextFileStatus.FLAGGED: "⚠",
            ContextFileStatus.SHADOWED: "○",
            ContextFileStatus.BLOCKED: "✗",
            ContextFileStatus.EMPTY: "○",
            ContextFileStatus.UNREADABLE: "✗",
            ContextFileStatus.SUPPRESSED: "○",
        }[self]

    @property
    def description(self) -> str:
        return {
            ContextFileStatus.LOADED: "loaded",
            ContextFileStatus.TRUNCATED: "truncated — over context_file_max_chars",
            ContextFileStatus.FLAGGED: "loaded — matched prompt-injection pattern(s); review the file",
            ContextFileStatus.SHADOWED: "not loaded — higher-priority context type wins",
            ContextFileStatus.BLOCKED: "not loaded — blocked by the prompt-injection scan",
            ContextFileStatus.EMPTY: "not loaded — empty file",
            ContextFileStatus.UNREADABLE: "not loaded — could not be read",
            ContextFileStatus.SUPPRESSED: "not loaded — cwd fell back to the Sensei install tree",
        }[self]

    @property
    def loaded(self) -> bool:
        return self in (ContextFileStatus.LOADED, ContextFileStatus.TRUNCATED, ContextFileStatus.FLAGGED)


@dataclass(frozen=True)
class ContextFileEntry:
    """Single entry for the context file manifest."""
    label: str
    path: str
    chars: int
    est_tokens: int
    status: ContextFileStatus

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "path": self.path,
            "chars": self.chars,
            "est_tokens": self.est_tokens,
            "loaded": self.status.loaded,
            "status": self.status.value,
        }


def estimate_tokens_rough(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def load_context_file(path: Path, max_chars: int | None = None) -> tuple[str, ContextFileStatus]:
    """
    Load a context file with size limit.

    Returns (content, status). Content is empty string on error.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return "", ContextFileStatus.UNREADABLE

    if not content:
        return "", ContextFileStatus.EMPTY

    if max_chars is not None and len(content) > max_chars:
        return content[:max_chars], ContextFileStatus.TRUNCATED

    return content, ContextFileStatus.LOADED
