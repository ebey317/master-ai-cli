import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from main import main


def test_normal_greeting():
    assert main(["alice"]) == "Hello, alice!"


def test_shout_flag():
    assert main(["alice", "--shout"]) == "HELLO, ALICE!"
