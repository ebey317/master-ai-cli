"""Tests for the web-search MCP server.

The import fix: the module did `from mcp.server.fastmcp import FastMCP`,
which raises ModuleNotFoundError on mcp 2.x — where FastMCP was renamed to
MCPServer. The file could not be imported at all against the installed SDK,
so it could never be registered as a server or run standalone.

These assert the module imports and exposes the three tools it advertises.
They deliberately do not call the network: web_search needs SERPER_API_KEY
and wikipedia_search needs the internet, neither of which belongs in a
test. Call-path behaviour is covered by test_mcp_call.py against a stub
server.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def server():
    import mcp_web_search

    return mcp_web_search


def test_module_imports_on_the_installed_sdk(server):
    """The regression: this import raised ModuleNotFoundError on mcp 2.x."""
    assert server is not None


def test_it_resolves_a_server_class_across_mcp_versions(server):
    """Both SDK generations must work, so the file cannot break again on a
    pin change. mcp >= 2 names it MCPServer, mcp 1 names it FastMCP."""
    assert server._Server.__name__ in ("MCPServer", "FastMCP")


def test_the_three_advertised_tools_exist(server):
    for name in ("web_search", "wikipedia_search", "scrape_page"):
        assert callable(getattr(server, name)), name


def test_key_loader_reads_the_shared_keyfile(server, tmp_path, monkeypatch):
    """SERPER_API_KEY comes from ~/.master_ai_keys, shared with the agent."""
    keys = tmp_path / ".master_ai_keys"
    keys.write_text('{"SERPER_API_KEY": "abc123"}')
    monkeypatch.setattr(server.Path, "home", staticmethod(lambda: tmp_path))
    assert server._load_key("SERPER_API_KEY", "SERPER_API_KEY") == "abc123"


def test_key_loader_falls_back_to_key_value_lines(server, tmp_path, monkeypatch):
    keys = tmp_path / ".master_ai_keys"
    keys.write_text('SERPER_API_KEY="xyz789"\n')
    monkeypatch.setattr(server.Path, "home", staticmethod(lambda: tmp_path))
    assert server._load_key("SERPER_API_KEY", "SERPER_API_KEY") == "xyz789"


def test_key_loader_returns_none_when_absent(server, tmp_path, monkeypatch):
    keys = tmp_path / ".master_ai_keys"
    keys.write_text("SOMETHING_ELSE=1\n")
    monkeypatch.setattr(server.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    assert server._load_key("SERPER_API_KEY", "SERPER_API_KEY") is None
