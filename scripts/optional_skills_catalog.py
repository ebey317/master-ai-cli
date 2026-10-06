"""
Optional skills catalog and installer for Sensei.
Manages optional-skills/ directory and registers blueprints as suggestions.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .skill_metadata import SkillBlueprint, SkillManifest


@dataclass
class SuggestionEntry:
    """A user-opt-in suggestion registered from a skill blueprint."""

    skill_name: str
    skill_version: str
    blueprint: SkillBlueprint
    registered_at: str  # ISO timestamp
    accepted: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["blueprint"] = asdict(self.blueprint)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SuggestionEntry:
        bp_data = data.pop("blueprint")
        data["blueprint"] = SkillBlueprint(**bp_data)
        return cls(**data)


class OptionalSkillsCatalog:
    """Manages optional skills installation and blueprint suggestions."""

    def __init__(self, sensei_home: Path):
        self.sensei_home = sensei_home
        self.optional_skills_dir = sensei_home / "optional-skills"
        self.installed_skills_dir = sensei_home / "skills" / "installed"
        self.suggestions_file = sensei_home / "suggestions.json"
        self.dashboards_dir = sensei_home / "dashboards"

        # Ensure directories exist
        self.optional_skills_dir.mkdir(parents=True, exist_ok=True)
        self.installed_skills_dir.mkdir(parents=True, exist_ok=True)
        self.dashboards_dir.mkdir(parents=True, exist_ok=True)

    def list_available(self) -> list[SkillManifest]:
        """List all available optional skills."""
        skills = []
        for category_dir in self.optional_skills_dir.iterdir():
            if category_dir.is_dir():
                for skill_dir in category_dir.iterdir():
                    if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                        try:
                            skills.append(SkillManifest.from_directory(skill_dir))
                        except Exception:
                            pass  # Skip invalid skills
        return skills

    def list_installed(self) -> list[SkillManifest]:
        """List all installed skills."""
        skills = []
        for skill_dir in self.installed_skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                try:
                    skills.append(SkillManifest.from_directory(skill_dir))
                except Exception:
                    pass
        return skills

    def install(self, skill_ref: str) -> SkillManifest:
        """
        Install an optional skill by reference (e.g., 'productivity/live-dashboard').
        Registers its blueprint as a suggestion (opt-in).
        """
        # Parse reference: category/skill-name
        parts = skill_ref.split("/")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid skill reference: {skill_ref}. Use 'category/skill-name'"
            )

        category, skill_name = parts
        source_dir = self.optional_skills_dir / category / skill_name

        if not source_dir.exists():
            raise FileNotFoundError(f"Skill not found: {skill_ref}")

        manifest = SkillManifest.from_directory(source_dir)
        target_dir = self.installed_skills_dir / manifest.slug

        if target_dir.exists():
            shutil.rmtree(target_dir)

        # Copy skill to installed directory
        shutil.copytree(source_dir, target_dir)

        # Register blueprint as suggestion if present
        if manifest.metadata.blueprint:
            self._register_suggestion(manifest)

        return manifest

    def uninstall(self, skill_slug: str) -> bool:
        """Uninstall a skill and remove its suggestions."""
        target_dir = self.installed_skills_dir / skill_slug
        if not target_dir.exists():
            return False

        shutil.rmtree(target_dir)
        self._remove_suggestions(skill_slug)
        return True

    def _register_suggestion(self, manifest: SkillManifest) -> None:
        """Register skill's blueprint as a user-opt-in suggestion."""
        suggestions = self._load_suggestions()

        # Remove any existing suggestion for this skill
        suggestions = [s for s in suggestions if s.skill_name != manifest.metadata.name]

        from datetime import datetime

        entry = SuggestionEntry(
            skill_name=manifest.metadata.name,
            skill_version=manifest.metadata.version,
            blueprint=manifest.metadata.blueprint,  # type: ignore[arg-type]
            registered_at=datetime.now().isoformat(),
            accepted=False,
        )
        suggestions.append(entry)
        self._save_suggestions(suggestions)

    def _remove_suggestions(self, skill_slug: str) -> None:
        suggestions = self._load_suggestions()
        suggestions = [s for s in suggestions if s.skill_name != skill_slug]
        self._save_suggestions(suggestions)

    def _load_suggestions(self) -> list[SuggestionEntry]:
        if not self.suggestions_file.exists():
            return []
        try:
            data = json.loads(self.suggestions_file.read_text())
            return [SuggestionEntry.from_dict(d) for d in data]
        except Exception:
            return []

    def _save_suggestions(self, suggestions: list[SuggestionEntry]) -> None:
        self.suggestions_file.write_text(
            json.dumps([s.to_dict() for s in suggestions], indent=2)
        )

    def get_pending_suggestions(self) -> list[SuggestionEntry]:
        """Get all unaccepted suggestions for /suggestions command."""
        return [s for s in self._load_suggestions() if not s.accepted]

    def accept_suggestion(self, skill_name: str) -> bool:
        """Mark a suggestion as accepted (user opted in)."""
        suggestions = self._load_suggestions()
        for s in suggestions:
            if s.skill_name == skill_name:
                s.accepted = True
                self._save_suggestions(suggestions)
                return True
        return False

    def get_dashboard_path(self, slug: str) -> Path:
        """Get absolute path for a dashboard (never hardcoded ~)."""
        return self.dashboards_dir / slug / "index.html"

    def get_dashboard_state_path(self, slug: str) -> Path:
        """Get absolute path for dashboard state file."""
        return self.dashboards_dir / slug / "state.json"
