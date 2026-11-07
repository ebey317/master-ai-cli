import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from expr import evaluate


def test_literal():
    assert evaluate("3", {}) == 3


def test_variable():
    assert evaluate("x", {"x": 7}) == 7


def test_arithmetic():
    assert evaluate("2 + 3 * 4", {}) == 14


def test_parentheses():
    assert evaluate("(2 + 3) * 4", {}) == 20


def test_division():
    assert evaluate("10 / 4", {}) == 2.5


def test_mixed():
    assert evaluate("a + b * (c - 1)", {"a": 1, "b": 2, "c": 4}) == 7
