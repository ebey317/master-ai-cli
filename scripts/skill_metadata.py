"""
Skill metadata parser with blueprint support for Sensei.
Portable design: skills declare cron/suggestion blueprints in their SKILL.md frontmatter.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
import yaml


@dataclass
class SkillBlueprint:
    """A scheduled job/suggestion declared by a skill."""
    schedule: str  # cron expression
    prompt: str    # prompt template for the suggestion
    description: str = ""
    opt_in: bool = True  # never auto-scheduled, user must accept via /suggestions


@dataclass
class SkillMetadata:
    """Parsed skill metadata from SKILL.md frontmatter."""
    name: str
    description: str
    version: str
    author: str
    license: str
    platforms: list[str]
    tags: list[str] = field(default_factory=list)
    related_skills: list[str] = field(default_factory=list)
    blueprint: Optional[SkillBlueprint] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, skill_path: Path) -> "SkillMetadata":
        """Parse SKILL.md frontmatter."""
        content = skill_path.read_text(encoding="utf-8")
        frontmatter, _ = cls._split_frontmatter(content)
        data = yaml.safe_load(frontmatter) or {}

        # Extract hermes-specific metadata
        hermes_meta = data.get("metadata", {}).get("hermes", {})
        blueprint_data = hermes_meta.get("blueprint")

        blueprint = None
        if blueprint_data:
            blueprint = SkillBlueprint(
                schedule=blueprint_data.get("schedule", "0 8 * * *"),
                prompt=blueprint_data.get("prompt", ""),
                description=blueprint_data.get("description", ""),
                opt_in=blueprint_data.get("opt_in", True),
            )

        return cls(
            name=data.get("name", skill_path.parent.name),
            description=data.get("description", ""),
            version=data.get("version", "0.1.0"),
            author=data.get("author", "Unknown"),
            license=data.get("license", "MIT"),
            platforms=data.get("platforms", ["linux", "macos", "windows"]),
            tags=hermes_meta.get("tags", []),
            related_skills=hermes_meta.get("related_skills", []),
            blueprint=blueprint,
            raw=data,
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[str, str]:
        """Split YAML frontmatter from markdown body."""
        match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
        if match:
            return match.group(1), match.group(2)
        return "", content


@dataclass
class SkillManifest:
    """Complete skill manifest including runtime paths."""
    metadata: SkillMetadata
    skill_dir: Path
    entry_point: Optional[Path] = None  # e.g., main.py, skill.py

    @property
    def slug(self) -> str:
        return self.metadata.name.lower().replace(" ", "-")

    @classmethod
    def from_directory(cls, skill_dir: Path) -> "SkillManifest":
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            raise FileNotFoundError(f"No SKILL.md in {skill_dir}")

        metadata = SkillMetadata.from_file(skill_md)

        # Find entry point
        entry_point = None
        for candidate in ("main.py", "skill.py", f"{metadata.name}.py", "run.py"):
            p = skill_dir / candidate
            if p.exists():
                entry_point = p
                break

        return cls(metadata=metadata, skill_dir=skill_dir, entry_point=entry_point)
