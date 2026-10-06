"""Tests for the unified tool-search helper."""

import pytest

from scripts.tools.unified_search import ToolEntry, TooManyQueriesError, search


def _entry(name: str, text: str, source: str = "local") -> ToolEntry:
    return ToolEntry(name=name, search_text=text, source=source)


def test_connector_tools_compete_with_local_tools():
    """Connector entries must not be relegated to leftover slots."""
    local = [
        _entry("betterstack_incident_list", "list betterstack incidents monitoring"),
        _entry("betterstack_status_page", "betterstack status page uptime"),
        _entry("betterstack_heartbeat", "betterstack heartbeat check"),
        _entry("betterstack_monitor", "betterstack monitor alerts"),
        _entry("betterstack_something_else", "betterstack something else"),
    ]
    connector = [
        _entry(
            "connectors__gmail__CREATE_EMAIL_DRAFT",
            "gmail create email draft send message google mail",
            source="connector",
        ),
        _entry(
            "connectors__gmail__SEND_EMAIL",
            "gmail send email message google mail",
            source="connector",
        ),
    ]

    results = search(["send gmail email"], local, connector, limit=5)

    names = [e.name for e in results["send gmail email"]]
    assert "connectors__gmail__SEND_EMAIL" in names
    # Local tools with weak overlap should not crowd out the connector hit.
    assert "betterstack_something_else" not in names


def test_limit_is_total_not_per_source():
    local = [_entry("local_a", "alpha beta"), _entry("local_b", "alpha beta")]
    connector = [_entry("conn_a", "alpha beta", source="connector")]
    results = search(["alpha"], local, connector, limit=2)
    assert len(results["alpha"]) == 2


def test_zero_overlap_excludes_entry():
    local = [_entry("local_a", "foo bar")]
    connector = [_entry("conn_a", "baz qux", source="connector")]
    results = search(["foo"], local, connector)
    assert results["foo"] == [local[0]]


def test_too_many_queries_raises():
    with pytest.raises(TooManyQueriesError):
        search(["q"] * 8, [], [], max_queries_per_call=7)
