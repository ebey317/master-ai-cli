"""
Desktop preview integration for Sensei.
Portable design: render HTML in preview pane when desktop_preview tool available,
otherwise report absolute file path.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional


class DesktopPreview:
    """Handles dashboard preview in desktop vs terminal contexts."""

    def __init__(self, dashboards_dir: Path):
        self.dashboards_dir = dashboards_dir
        self._has_desktop_preview = False  # Set by runtime when tool available

    def set_desktop_preview_available(self, available: bool) -> None:
        """Called by runtime when desktop_preview tool is in toolset."""
        self._has_desktop_preview = available

    def show_dashboard(self, slug: str) -> str:
        """
        Show dashboard in preview pane (desktop) or return absolute path (terminal).
        Returns user-facing message.
        """
        dashboard_path = self.dashboards_dir / slug / "index.html"

        if not dashboard_path.exists():
            return f"[ERROR] Dashboard not found: {dashboard_path}"

        if self._has_desktop_preview:
            # In desktop app: render in preview pane
            # This would call the desktop_preview tool with the HTML content
            html_content = dashboard_path.read_text(encoding="utf-8")
            return f"[DESKTOP_PREVIEW] {html_content}"
        else:
            # In terminal: report absolute path
            abs_path = dashboard_path.resolve()
            return f"Dashboard available at: file://{abs_path}"

    def render_after_build(self, slug: str) -> str:
        """Called after dashboard build/tick to auto-preview in desktop."""
        return self.show_dashboard(slug)
