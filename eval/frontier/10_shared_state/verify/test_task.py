import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from registry import PluginRegistry


def test_instances_are_independent():
    a = PluginRegistry()
    b = PluginRegistry()
    a.register("x")
    b.register("y")
    assert a.list() == ["x"]
    assert b.list() == ["y"]
