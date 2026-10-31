"""Offline tests for the lane-health / pin-independence work (2026-10-22).

No network. These cover the parts that decide WHICH lane gets called and
IN WHAT ORDER — the logic that used to make a 122-second lane sit ahead of
a 1.3-second one and cost every question two minutes.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _use_temp_health(tmpdir):
    import lane_health

    lane_health.HEALTH_FILE = Path(tmpdir) / "lane_health.json"
    return lane_health


def test_unknown_lanes_keep_operator_order():
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        assert lh.rank(["opencode", "nvidia", "nemotron"]) == [
            "opencode",
            "nvidia",
            "nemotron",
        ]


def test_measured_fast_lane_jumps_the_queue():
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        lh.record("nemotron", True, 122.0)
        lh.record("nvidia", True, 1.3)
        assert lh.rank(["opencode", "nvidia", "nemotron"])[0] == "nvidia"


def test_proven_dead_lane_is_marked_bad_and_ranked_last():
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        for _ in range(4):
            lh.record("opencode", False, 0.2)
        snap = lh.snapshot()
        assert snap["opencode"]["bad"] is True
        assert lh.is_known_bad("opencode") is True
        ranked = lh.rank(["opencode", "nvidia", "nemotron"])
        assert ranked[-1] == "opencode"


def test_one_failure_does_not_condemn_a_lane():
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        lh.record("nvidia", False, 0.2)
        assert lh.is_known_bad("nvidia") is False


def test_known_fast_requires_repeated_success_and_low_latency():
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        lh.record("nvidia", True, 1.3)
        assert lh.known_fast("nvidia", 10.0) is False  # one sample is not proof
        lh.record("nvidia", True, 1.4)
        assert lh.known_fast("nvidia", 10.0) is True
        lh.record("nvidia", True, 40.0)
        assert lh.known_fast("nvidia", 10.0) is False  # now too slow


def test_recent_samples_outweigh_old_ones():
    """A lane that got faster must be re-scored, not stuck on its history."""
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        for _ in range(8):
            lh.record("lane", True, 30.0)
        slow = lh.snapshot()["lane"]["ewma_latency"]
        for _ in range(12):
            lh.record("lane", True, 1.0)
        fast = lh.snapshot()["lane"]["ewma_latency"]
        assert fast < slow
        assert fast < 5.0


def test_best_alternative_skips_dead_and_prefers_proven():
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        for _ in range(4):
            lh.record("dead", False, 0.1)
        lh.record("fast", True, 1.0)
        lh.record("slow", True, 90.0)
        best = lh.best_alternative(["dead", "slow", "fast", "unknown"])
        assert best == "fast"


def test_best_alternative_respects_exclusions():
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        lh.record("a", True, 1.0)
        lh.record("b", True, 2.0)
        assert lh.best_alternative(["a", "b"], exclude={"a"}) == "b"


def test_health_file_survives_a_reload():
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        lh.record("nvidia", True, 1.3)
        raw = json.loads(Path(lh.HEALTH_FILE).read_text())
        assert "nvidia" in raw["lanes"]


def test_broken_health_file_does_not_crash():
    with tempfile.TemporaryDirectory() as td:
        lh = _use_temp_health(td)
        Path(lh.HEALTH_FILE).write_text("{not json")
        assert lh.snapshot() == {}
        assert lh.rank(["a", "b"]) == ["a", "b"]


def test_master_ai_exposes_the_new_knobs():
    import master_ai

    assert master_ai._CLOUD_TURN_BUDGET_S > 0
    assert master_ai._HEDGE_SLOWER_THAN_S > 0
    # the chain must have a budget, not a per-hop 150s
    assert master_ai._CLOUD_TURN_BUDGET_S < master_ai._CLOUD_HARD_TIMEOUT


def test_ollama_cloud_is_a_real_chain_lane():
    """The fast lane must be reachable as a FALLBACK, not only as a pin —
    otherwise pinning something else loses the fast lane entirely."""
    from sensei_tables import _VALID_FALLBACK_NAMES, ASK_CLOUD_BARE_PROVIDERS

    assert "ollama-cloud" in ASK_CLOUD_BARE_PROVIDERS
    assert "ollama-cloud" in _VALID_FALLBACK_NAMES


def test_backup_pool_respects_the_operator_saved_chain():
    """A race must not start spending on a lane the operator never put in
    their chain."""
    import master_ai

    fn_map = {
        "in-chain": object(),
        "not-in-chain": object(),
    }
    original = master_ai._load_fallback_order
    master_ai._load_fallback_order = lambda: ["in-chain"]
    try:
        picked = master_ai._pick_backup_lanes(fn_map, exclude=(), count=3)
    finally:
        master_ai._load_fallback_order = original
    assert "not-in-chain" not in picked
    assert "in-chain" in picked


def test_backup_pool_excludes_the_same_provider_as_the_pin():
    """Pin "ollama-cloud::x" must not race against the "ollama-cloud" lane —
    that is the same provider, and would bill twice for one answer."""
    import master_ai

    fn_map = {"ollama-cloud": object(), "nvidia": object()}
    original = master_ai._load_fallback_order
    master_ai._load_fallback_order = lambda: ["ollama-cloud", "nvidia"]
    try:
        picked = master_ai._pick_backup_lanes(
            fn_map, exclude=("ollama-cloud::nemotron-3-super",), count=3
        )
    finally:
        master_ai._load_fallback_order = original
    assert "ollama-cloud" not in picked
    assert "nvidia" in picked


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:
                failures += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print()
    print("FAILED" if failures else "ALL PASS")
    sys.exit(1 if failures else 0)
