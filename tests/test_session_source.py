"""Tests for session source resolution."""

import os
import pytest

from scripts.session_source import (
    resolve_session_source,
    mark_single_query_session,
    set_session_source,
    SESSION_SOURCE_ENV,
    SINGLE_QUERY_SESSION_ENV,
    _UI_TRANSPORT_SOURCES,
    _AUTOMATION_SOURCES,
)


def test_session_source_falls_back_to_platform(monkeypatch):
    """No inherited source -> falls back to platform."""
    monkeypatch.delenv(SESSION_SOURCE_ENV, raising=False)
    monkeypatch.delenv(SINGLE_QUERY_SESSION_ENV, raising=False)

    assert resolve_session_source("tui") == "tui"
    assert resolve_session_source("cli") == "cli"
    assert resolve_session_source(None) == "cli"


@pytest.mark.parametrize("inherited", ["tui", "desktop"])
def test_oneshot_child_drops_inherited_ui_transport_source(monkeypatch, inherited):
    """A finite one-shot spawned from a TUI/Desktop session inherits the transport's
    SESSION_SOURCE but is not that conversation: it keeps its own platform label."""
    monkeypatch.setenv(SESSION_SOURCE_ENV, inherited)
    monkeypatch.setenv(SINGLE_QUERY_SESSION_ENV, "1")

    assert resolve_session_source("cli") == "cli"


@pytest.mark.parametrize("inherited", ["kanban", "tool", "cron", "a2a"])
def test_oneshot_child_keeps_inherited_automation_source(monkeypatch, inherited):
    """Automation sources are inherited on purpose by one-shot children."""
    monkeypatch.setenv(SESSION_SOURCE_ENV, inherited)
    monkeypatch.setenv(SINGLE_QUERY_SESSION_ENV, "1")

    assert resolve_session_source("cli") == inherited


def test_oneshot_child_without_marker_keeps_inherited_ui_source(monkeypatch):
    """Without the single-query marker, UI transport source is inherited (normal child)."""
    monkeypatch.setenv(SESSION_SOURCE_ENV, "tui")
    monkeypatch.delenv(SINGLE_QUERY_SESSION_ENV, raising=False)

    assert resolve_session_source("cli") == "tui"


def test_explicit_source_override(monkeypatch):
    """Explicit --source flag (via set_session_source) takes precedence."""
    monkeypatch.setenv(SESSION_SOURCE_ENV, "tui")
    monkeypatch.setenv(SINGLE_QUERY_SESSION_ENV, "1")
    # Simulate explicit --source tool
    set_session_source("tool")

    assert resolve_session_source("cli") == "tool"


def test_mark_single_query_session_sets_env(monkeypatch):
    """mark_single_query_session sets the marker and non-interactive flags."""
    monkeypatch.delenv(SINGLE_QUERY_SESSION_ENV, raising=False)
    monkeypatch.delenv("SENSEI_YOLO_MODE", raising=False)
    monkeypatch.delenv("SENSEI_ACCEPT_HOOKS", raising=False)

    mark_single_query_session()

    assert os.environ[SINGLE_QUERY_SESSION_ENV] == "1"
    assert os.environ["SENSEI_YOLO_MODE"] == "1"
    assert os.environ["SENSEI_ACCEPT_HOOKS"] == "1"
