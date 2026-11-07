import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from service import get_user_summary


def test_user_summary():
    raw = {"name": "van Devere", "age": " 42 "}
    assert get_user_summary(raw) == "van Devere is 42 years old"
