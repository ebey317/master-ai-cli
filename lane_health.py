"""Lane health: which cloud lane is fast and alive *right now*.

Why this exists (2026-10-22)
----------------------------
Elijah's requirement: the CLI's speed must not depend on which model is
pinned. The CLI was already MEASURING this — `_router_metric()` writes
per-lane latency and success to `~/.master_ai_router_metrics.jsonl` on
every call — but nothing ever read that file back. The fallback chain was
a hand-maintained static list, so it rotted:

  * a 122s OpenRouter free-tier lane sat AHEAD of a 1.3s NVIDIA lane, so
    every question paid 122s before reaching the fast one;
  * a lane whose model id had been deprecated upstream still got called on
    every single turn.

So the fix is not "pick a better model" — it is "let the CLI learn which
lane is fast and skip the ones that are dead." This module is the memory
for that: a tiny JSON file, O(1) to update, no parsing of the big metrics
log on the hot path.

Model-independence comes from three places, all built on this file:

  1. `rank()` orders the fallback chain by measured health, so the fast
     lane is tried first no matter where it sits in the operator's saved
     order.
  2. `known_fast()` decides whether a pinned lane needs a hedged backup
     racing alongside it, so pinning a slow model stops costing latency.
  3. `is_known_bad()` skips a lane that has failed every single recent
     attempt, so a dead lane costs ~0ms instead of a full timeout.

Storage
-------
`~/.master_ai_lane_health.json`

    {"lanes": {"cloud/nvidia": {"samples": [[1, 1.33], [0, 0.2]], "last_ts": 1792694839}}}

Each sample is [ok(0|1), latency_s]. Only the most recent
`MAX_SAMPLES` are kept, so the file stays small and old evidence decays
out on its own — a lane that used to be slow and is fast now gets a fresh
score without any manual reset.
"""

import json
import os
import time
from pathlib import Path

HEALTH_FILE = Path.home() / ".master_ai_lane_health.json"

MAX_SAMPLES = 12
# A lane needs this many attempts before "failed every time" counts as
# dead. Below that, one bad call (a network blip) is not a verdict.
MIN_SAMPLES_FOR_DEAD = 3
# Below this success rate a lane is treated as failing.
OK_RATE_FLOOR = 0.5


def _now() -> float:
    return time.time()


def _load() -> dict:
    try:
        data = json.loads(HEALTH_FILE.read_text())
        if isinstance(data, dict) and isinstance(data.get("lanes"), dict):
            return data
    except Exception:
        pass
    return {"lanes": {}}


def _save(data: dict) -> None:
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, HEALTH_FILE)
    except Exception:
        pass  # health data is an optimisation, never a hard failure


def record(lane: str, ok: bool, latency_s: float) -> None:
    """Remember how one call to `lane` went. Never raises."""
    if not lane:
        return
    try:
        data = _load()
        lanes = data.setdefault("lanes", {})
        entry = lanes.setdefault(lane, {"samples": [], "last_ts": 0})
        samples = entry.setdefault("samples", [])
        try:
            lat = max(0.0, float(latency_s))
        except Exception:
            lat = 0.0
        samples.append([1 if ok else 0, round(lat, 3)])
        if len(samples) > MAX_SAMPLES:
            del samples[: len(samples) - MAX_SAMPLES]
        entry["last_ts"] = int(_now())
        _save(data)
    except Exception:
        pass


def snapshot() -> dict:
    """{lane: {"n": int, "ok_rate": float, "ewma_latency": float, "bad": bool}}"""
    out = {}
    try:
        lanes = _load().get("lanes", {})
    except Exception:
        lanes = {}
    for lane, entry in lanes.items():
        samples = entry.get("samples") or []
        if not samples:
            continue
        n = len(samples)
        oks = sum(1 for s in samples if s and s[0])
        ok_rate = oks / n
        # Recency-weighted mean latency: later samples count more, so a
        # lane that got faster (or slower) is re-scored within a couple
        # of turns instead of staying stuck on its historical average.
        num = 0.0
        den = 0.0
        for i, s in enumerate(samples):
            w = i + 1
            try:
                lat = float(s[1])
            except Exception:
                lat = 0.0
            num += lat * w
            den += w
        ewma = num / den if den else 0.0
        out[lane] = {
            "n": n,
            "ok_rate": round(ok_rate, 3),
            "ewma_latency": round(ewma, 3),
            "bad": n >= MIN_SAMPLES_FOR_DEAD and ok_rate <= 0.0,
        }
    return out


def score(lane: str) -> float:
    """Lower is better. Unknown lanes get a large-but-finite score so they
    are tried, just after the lanes proven fast."""
    snap = snapshot()
    info = snap.get(lane)
    if not info:
        return 1e6
    if info["bad"]:
        return 1e9
    # A lane that fails half the time is penalised, but a single failure
    # does not send it to the back of the queue.
    penalty = 1.0 + 4.0 * (1.0 - info["ok_rate"])
    return max(0.001, info["ewma_latency"] * penalty)


def rank(lanes) -> list:
    """Order `lanes` by measured health. The caller's original order is the
    tiebreak for lanes we have no data on, so the operator's saved chain
    order still means something — it just stops overriding what the CLI
    has actually measured."""
    lanes = list(lanes)
    snap = snapshot()

    def bucket(lane):
        info = snap.get(lane)
        if not info:
            return 1  # unknown
        if info["bad"]:
            return 2  # proven dead
        return 0  # proven alive

    indexed = list(enumerate(lanes))
    indexed.sort(key=lambda t: (bucket(t[1]), score(t[1]), t[0]))
    return [lane for _, lane in indexed]


def known_fast(lane: str, max_latency_s: float = 10.0) -> bool:
    """True when this lane has proven itself: repeated successes, and a
    recent mean latency we are happy to wait for. A lane that has never
    been tried is NOT known-fast — we would rather race a backup against
    it than bet the turn on it."""
    info = snapshot().get(lane)
    if not info:
        return False
    if info["n"] < 2:
        return False
    if info["ok_rate"] < 0.8:
        return False
    return info["ewma_latency"] <= max_latency_s


def known_slow(lane: str, min_latency_s: float = 10.0) -> bool:
    """True when this lane has a history of being slow (or failing) badly
    enough that racing a backup against it is worth the extra call.

    Deliberately FALSE for an unknown lane. An unknown lane has not earned
    the right to be replaced by a different model — and a request whose
    REPLY SHAPE matters (e.g. one that must emit a <PLAN> block) can be
    broken by a fast lane answering instead of the routed one."""
    info = snapshot().get(lane)
    if not info:
        return False
    if info["n"] < 2:
        return False
    if info["ok_rate"] < 0.8:
        return True
    return info["ewma_latency"] > min_latency_s


def is_known_bad(lane: str) -> bool:
    info = snapshot().get(lane)
    return bool(info and info["bad"])


def best_alternative(lanes, exclude=()) -> str:
    """The fastest lane in `lanes` that is not in `exclude` and is not
    proven dead. Used to pick the hedged backup for a slow/unproven pin."""
    cands = [l for l in lanes if l not in exclude and not is_known_bad(l)]
    if not cands:
        return ""
    ordered = rank(cands)
    # Prefer a lane we have actually measured; fall back to the operator's
    # order only if nothing has data yet.
    snap = snapshot()
    measured = [l for l in ordered if l in snap]
    return (measured or ordered)[0]
