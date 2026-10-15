"""Tests for the plugin catalog loader."""

import tempfile
import textwrap
from pathlib import Path

import pytest

from scripts.plugin_catalog import PluginCatalog, PluginType

SAMPLE_CATALOG = textwrap.dedent("""\
    schema_version: 1
    plugins:
      - name: "test-skill"
        type: "skill"
        description: "A test skill"
        entry_point: "mod:Class"
        capabilities: ["cap1", "cap2"]
        requires_env: ["TEST_VAR"]
        tier: "core"
        metadata:
          complexity: "low"
          typical_latency_ms: 100
      - name: "test-tool"
        type: "tool"
        description: "A test tool"
        entry_point: "mod:func"
        capabilities: ["cap2"]
        requires_env: []
        tier: "community"
""")


def test_load_catalog():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_CATALOG)
        path = Path(f.name)
    try:
        catalog = PluginCatalog(path)
        assert len(catalog.all()) == 2
        assert catalog.get("test-skill") is not None
        assert catalog.get("test-tool") is not None
        assert catalog.get("missing") is None
    finally:
        path.unlink()


def test_filter_by_type():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_CATALOG)
        path = Path(f.name)
    try:
        catalog = PluginCatalog(path)
        skills = catalog.find_by_type(PluginType.SKILL)
        tools = catalog.find_by_type(PluginType.TOOL)
        assert len(skills) == 1
        assert skills[0].name == "test-skill"
        assert len(tools) == 1
        assert tools[0].name == "test-tool"
    finally:
        path.unlink()


def test_filter_by_capability():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_CATALOG)
        path = Path(f.name)
    try:
        catalog = PluginCatalog(path)
        cap1 = catalog.find_by_capability("cap1")
        cap2 = catalog.find_by_capability("cap2")
        assert len(cap1) == 1
        assert cap1[0].name == "test-skill"
        assert len(cap2) == 2
        names = {p.name for p in cap2}
        assert names == {"test-skill", "test-tool"}
    finally:
        path.unlink()


def test_env_satisfied():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_CATALOG)
        path = Path(f.name)
    try:
        catalog = PluginCatalog(path)
        skill = catalog.get("test-skill")
        tool = catalog.get("test-tool")

        # skill requires TEST_VAR
        ok, missing = skill.env_satisfied()
        assert ok is False
        assert missing == ["TEST_VAR"]

        # tool requires nothing
        ok, missing = tool.env_satisfied()
        assert ok is True
        assert missing == []
    finally:
        path.unlink()


def test_available_filters_by_env():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_CATALOG)
        path = Path(f.name)
    try:
        catalog = PluginCatalog(path)
        # Without TEST_VAR, only tool should be available
        available = catalog.available()
        assert len(available) == 1
        assert available[0].name == "test-tool"
    finally:
        path.unlink()


def test_invalid_schema_version():
    bad_catalog = "schema_version: 2\nplugins: []\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(bad_catalog)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="Unsupported catalog schema version"):
            PluginCatalog(path)
    finally:
        path.unlink()
