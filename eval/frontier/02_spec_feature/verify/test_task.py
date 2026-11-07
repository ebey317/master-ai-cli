import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from config import Config


def test_typed_values(tmp_path):
    p = tmp_path / "cfg.env"
    p.write_text("""
# comment
name = alice
timeout = 30
ratio = 3.14
enabled = true
disabled = False
""")
    cfg = Config(p).load()
    assert cfg.data["name"] == "alice"
    assert cfg.data["timeout"] == 30
    assert cfg.data["ratio"] == 3.14
    assert cfg.data["enabled"] is True
    assert cfg.data["disabled"] is False
