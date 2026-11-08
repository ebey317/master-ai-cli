import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from api import register


def test_register_valid():
    assert register("alice@example.com", "alice123") is True


def test_register_invalid_email():
    assert register("not-an-email", "alice123") is False


def test_register_invalid_username():
    assert register("alice@example.com", "a!") is False


def test_helpers_removed():
    repo = Path(__file__).resolve().parents[1] / "repo"
    assert not (repo / "helpers.py").exists()
