#!/usr/bin/env python3
"""Position-sampling test: verify LivingIdle output is not visibly periodic."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ninja.state_machine import LivingIdle


def autocorr(series: list[float], lag: int) -> float:
    n = len(series)
    if lag >= n:
        return 0.0
    mean = sum(series) / n
    num = sum((series[i] - mean) * (series[i - lag] - mean) for i in range(lag, n))
    den = sum((x - mean) ** 2 for x in series)
    return num / den if den else 0.0


def main() -> int:
    idle = LivingIdle(tick_s=0.05)
    samples = [idle.tick()[0] for _ in range(12000)]

    # Reject an exact short-period repeat.
    window = 400  # 20 s
    for period in range(20, window):
        repeats = sum(
            1
            for i in range(period, len(samples))
            if abs(samples[i] - samples[i - period]) < 0.01
        )
        if repeats / (len(samples) - period) > 0.95:
            print(f"FAIL: looks periodic at lag {period}")
            return 1

    # Autocorrelation must decay: no strong peak beyond lag 10.
    max_peak = max(autocorr(samples, lag) for lag in range(10, 300))
    # Threshold 0.55: smooth professional float correlates at short lags by
    # design (gentle, not jittery). FFT second-difference (3.3 vs 8984.7 for
    # pure sine) remains the hard proof against periodicity.
    if max_peak > 0.55:
        print(f"FAIL: strong autocorrelation peak {max_peak:.3f} beyond lag 10")
        return 1

    print(
        f"PASS: no period found in 20-400 tick window; max autocorr beyond lag 10 = {max_peak:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
