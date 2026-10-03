"""
Tests for skill metadata blueprint parsing and suggestion registration.
Invariants:
1. parse_blueprint roundtrip: frontmatter -> SkillBlueprint -> suggestion entry
2. desktop_preview path fallback: terminal mode returns absolute file:// URL
"""
import json
import tempfile
from pathlib import Path
from datetime import datetime

import pytest

from scripts.skill_metadata import SkillMetadata, SkillBlueprint, SkillManifest
from scripts.optional_skills_catalog import OptionalSkillsCatalog, SuggestionEntry
from scripts.desktop_preview import DesktopPreview


class TestSkillBlueprintParsing:
    """Invariant: blueprint frontmatter parses to SkillBlueprint correctly."""

    def test_parse_blueprint_from_frontmatter(self):
        frontmatter = """---
name: live-dashboard
description: "Build self-updating dashboards from live sources."
version: "0.2.0"
author: Sensei
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Dashboards, Monitoring]
    blueprint:
      schedule: "0 8 * * *"
      prompt: "Load the live-dashboard skill and run the refresh tick for every dashboard."
      description: "Daily all-dashboards sweep"
      opt_in: true
---
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(frontmatter)
            f.write("\n# Skill Body")
            skill_md = Path(f.name)

        try:
            metadata = SkillMetadata.from_file(skill_md)
            assert metadata.name == "live-dashboard"
            assert metadata.version == "0.2.0"
            assert metadata.blueprint is not None
            assert metadata.blueprint.schedule == "0 8 * * *"
            assert "refresh tick" in metadata.blueprint.prompt
            assert metadata.blueprint.opt_in is True
        finally:
            skill_md.unlink()

    def test_blueprint_optional_defaults_to_true(self):
        """Blueprints are opt-in by default (never auto-scheduled)."""
        frontmatter = """---
name: test-skill
version: "1.0.0"
metadata:
  hermes:
    blueprint:
      schedule: "0 * * * *"
      prompt: "Test prompt"
---
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(frontmatter)
            skill_md = Path(f.name)

        try:
            metadata = SkillMetadata.from_file(skill_md)
            assert metadata.blueprint is not None
            assert metadata.blueprint.opt_in is True
        finally:
            skill_md.unlink()

    def test_skill_without_blueprint(self):
        """Skills without blueprint have None blueprint."""
        frontmatter = """---
name: simple-skill
version: "1.0.0"
---
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(frontmatter)
            skill_md = Path(f.name)

        try:
            metadata = SkillMetadata.from_file(skill_md)
            assert metadata.blueprint is None
        finally:
            skill_md.unlink()


class TestSuggestionRegistration:
    """Invariant: installing skill registers blueprint as unaccepted suggestion."""

    def test_install_skill_registers_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            sensei_home = Path(tmp)
            catalog = OptionalSkillsCatalog(sensei_home)

            # Create optional skill with blueprint
            skill_dir = catalog.optional_skills_dir / "productivity" / "live-dashboard"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("""---
name: live-dashboard
version: "0.2.0"
metadata:
  hermes:
    blueprint:
      schedule: "0 8 * * *"
      prompt: "Refresh all dashboards"
---
""")
            (skill_dir / "main.py").write_text("# dummy")

            # Install
            manifest = catalog.install("productivity/live-dashboard")

            # Verify suggestion registered
            suggestions = catalog.get_pending_suggestions()
            assert len(suggestions) == 1
            s = suggestions[0]
            assert s.skill_name == "live-dashboard"
            assert s.blueprint.schedule == "0 8 * * *"
            assert s.accepted is False

    def test_uninstall_removes_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            sensei_home = Path(tmp)
            catalog = OptionalSkillsCatalog(sensei_home)

            skill_dir = catalog.optional_skills_dir / "productivity" / "test-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("""---
name: test-skill
version: "1.0.0"
metadata:
  hermes:
    blueprint:
      schedule: "0 8 * * *"
      prompt: "Test"
---
""")
            catalog.install("productivity/test-skill")
            assert len(catalog.get_pending_suggestions()) == 1

            catalog.uninstall("test-skill")
            assert len(catalog.get_pending_suggestions()) == 0

    def test_accept_suggestion_marks_opted_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            sensei_home = Path(tmp)
            catalog = OptionalSkillsCatalog(sensei_home)

            skill_dir = catalog.optional_skills_dir / "cat" / "skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("""---
name: test-skill
version: "1.0.0"
metadata:
  hermes:
    blueprint:
      schedule: "0 8 * * *"
      prompt: "Test"
---
""")
            catalog.install("cat/skill")

            assert catalog.accept_suggestion("test-skill") is True
            suggestions = catalog.get_pending_suggestions()
            assert len(suggestions) == 0  # No longer pending

            # But still in full list
            all_suggestions = catalog._load_suggestions()
            assert len(all_suggestions) == 1
            assert all_suggestions[0].accepted is True


class TestDesktopPreviewFallback:
    """Invariant: desktop_preview renders in pane when available, else returns file:// path."""

    def test_terminal_mode_returns_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboards_dir = Path(tmp) / "dashboards"
            dashboards_dir.mkdir(parents=True)

            slug = "my-dashboard"
            dashboard_dir = dashboards_dir / slug
            dashboard_dir.mkdir()
            index_html = dashboard_dir / "index.html"
            index_html.write_text("<html><body>Dashboard</body></html>")

            preview = DesktopPreview(dashboards_dir)
            preview.set_desktop_preview_available(False)  # Terminal mode

            result = preview.show_dashboard(slug)
            assert "file://" in result
            assert str(index_html.resolve()) in result

    def test_desktop_mode_returns_html_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboards_dir = Path(tmp) / "dashboards"
            dashboards_dir.mkdir(parents=True)

            slug = "my-dashboard"
            dashboard_dir = dashboards_dir / slug
            dashboard_dir.mkdir()
            index_html = dashboard_dir / "index.html"
            html_content = "<html><body>Dashboard</body></html>"
            index_html.write_text(html_content)

            preview = DesktopPreview(dashboards_dir)
            preview.set_desktop_preview_available(True)  # Desktop mode

            result = preview.show_dashboard(slug)
            assert "[DESKTOP_PREVIEW]" in result
            assert html_content in result

    def test_missing_dashboard_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboards_dir = Path(tmp) / "dashboards"
            preview = DesktopPreview(dashboards_dir)

            result = preview.show_dashboard("nonexistent")
            assert "[ERROR]" in result
            assert "not found" in result


class TestDashboardPaths:
    """Invariant: dashboard paths use sensei home, never hardcoded ~."""

    def test_dashboard_path_uses_sensei_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            sensei_home = Path(tmp)
            catalog = OptionalSkillsCatalog(sensei_home)

            slug = "visa-applications"
            path = catalog.get_dashboard_path(slug)
            state_path = catalog.get_dashboard_state_path(slug)

            # Paths are under sensei_home/dashboards/
            assert sensei_home in path.parents
            assert sensei_home in state_path.parents
            assert path.name == "index.html"
            assert state_path.name == "state.json"
            assert "dashboards" in str(path)
            assert slug in str(path)

    def test_no_tilde_in_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            sensei_home = Path(tmp)
            catalog = OptionalSkillsCatalog(sensei_home)

            path = catalog.get_dashboard_path("test")
            assert "~" not in str(path)
            assert path.is_absolute()
