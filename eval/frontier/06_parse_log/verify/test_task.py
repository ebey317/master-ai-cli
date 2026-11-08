import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from parser import parse_log_line


def test_parse_full_line():
    line = (
        "[WARN] timestamp=2024-01-01T00:00:00 user=alice;retries=3 connection timeout"
    )
    out = parse_log_line(line)
    assert out["level"] == "WARN"
    assert out["timestamp"] == "2024-01-01T00:00:00"
    assert out["fields"] == {"user": "alice", "retries": "3"}
    assert out["message"] == "connection timeout"


def test_parse_no_fields():
    line = "[INFO] timestamp=2024-01-02T12:34:56 system boot completed"
    out = parse_log_line(line)
    assert out["level"] == "INFO"
    assert out["fields"] == {}
    assert out["message"] == "system boot completed"
