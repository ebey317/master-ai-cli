"""Operator drag anchor for the avatar overlay (2026-10-31).

The avatar used to be ungrabbable: pass-through in every non-approval
state, and the drift tick reset its position to the corner each frame.
The fix anchors the window where the operator drops it. The GTK drag
wiring is proven live; these tests pin the pure anchor logic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ninja import state_machine as sm  # noqa: E402


def test_anchor_round_trip(tmp_path):
    target = tmp_path / "overlay_pos.json"
    sm._save_user_anchor(640, 360, target)
    assert sm._load_user_anchor(target) == (640, 360)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"x": 640, "y": 360}


def test_anchor_missing_or_corrupt_is_none(tmp_path):
    assert sm._load_user_anchor(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert sm._load_user_anchor(bad) is None
    bad.write_text(json.dumps({"x": "left", "y": 3}), encoding="utf-8")
    assert sm._load_user_anchor(bad) is None


def test_resolve_base_prefers_the_operator_anchor():
    assert sm._resolve_base((10, 20), (1, 2)) == (10, 20)
    assert sm._resolve_base(None, (1, 2)) == (1, 2)
