"""Acceptance test: THE PIN MUST NOT DECIDE THE LATENCY.

Elijah's requirement (2026-10-22): the CLI's speed must not depend on
which model is pinned. Whatever model is pinned, a question must come back
in roughly the time of the fastest healthy lane.

This harness asks the SAME question under several different pins and
asserts the wall-clock is ~equal. It is expected to FAIL before the
latency work and PASS after it.

    python3 test_latency_agnostic.py            # live, real API calls
    python3 test_latency_agnostic.py --dry      # print plan, no calls

Pins under test:
  fast   - a healthy, quick lane            (ollama-cloud::nemotron-3-super)
  slow   - a healthy but SLOW lane          (nemotron = OpenRouter free tier)
  dead   - a model the upstream has killed   (opencode-go::minimax-m3.1-flash-free, 410)
  ghost  - a model id that never existed    (opencode-go::no-such-model-xyz)

"dead" and "ghost" are the ones that must cost ~0ms: a model that cannot
answer must not be able to make the turn slow.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PROMPT = [
    {"role": "user", "content": "Reply with exactly the word: PONG"},
]

PINS = [
    ("fast", "ollama-cloud::nemotron-3-super"),
    ("slow", "nemotron"),
    ("dead", "opencode-go::minimax-m3.1-flash-free"),
    ("ghost", "opencode-go::no-such-model-xyz"),
]

# The spread we are willing to tolerate between the fastest and slowest pin.
# Generous on purpose: this is a wall-clock assertion over the public
# internet, not a microbenchmark.
TOLERANCE_S = float(os.environ.get("LATENCY_TOLERANCE_S", "12"))


def main() -> int:
    dry = "--dry" in sys.argv
    import master_ai

    print("=" * 72)
    print("PIN INDEPENDENCE HARNESS")
    print("=" * 72)
    print(f"tolerance: fastest and slowest pin must be within {TOLERANCE_S:.1f}s")
    print()

    results = []
    for label, pin in PINS:
        if dry:
            print(f"  {label:6s} -> {pin}")
            continue
        t0 = time.time()
        try:
            answer = master_ai.ask_cloud(list(PROMPT), provider=pin)
        except Exception as e:  # a crash is a failure, not an excuse
            print(f"  {label:6s} -> {pin:42s} EXCEPTION {type(e).__name__}: {e}")
            answer = None
        dt = time.time() - t0
        who = getattr(master_ai, "_LAST_MODEL", None) or "-"
        ok = bool(answer)
        results.append((label, pin, dt, ok, who))
        flag = "OK " if ok else "EMPTY"
        print(f"  {label:6s} -> {pin:42s} {dt:7.2f}s  {flag}  answered_by={who}")

    if dry:
        print("\n(dry run - no calls made)")
        return 0

    print()
    answered = [r for r in results if r[3]]
    if not answered:
        print("FAIL: no pin produced an answer at all")
        return 1

    fastest = min(r[2] for r in results)
    slowest = max(r[2] for r in results)
    spread = slowest - fastest
    print(f"fastest pin : {fastest:.2f}s")
    print(f"slowest pin : {slowest:.2f}s")
    print(f"spread      : {spread:.2f}s (budget {TOLERANCE_S:.1f}s)")
    print()

    # A pin that cannot answer must not be able to slow the turn down.
    dead_slow = [r for r in results if r[0] in ("dead", "ghost") and r[2] > TOLERANCE_S]
    if dead_slow:
        for label, pin, dt, ok, who in dead_slow:
            print(f"FAIL: dead pin '{label}' ({pin}) took {dt:.2f}s")
        print("       a model that cannot answer must cost ~0ms")
        return 1

    if spread > TOLERANCE_S:
        print("FAIL: latency still depends on the pin")
        for label, pin, dt, ok, who in results:
            print(f"       {label:6s} {dt:7.2f}s  {pin}")
        return 1

    print("PASS: latency is independent of the pin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
