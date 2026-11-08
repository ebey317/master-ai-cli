import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo"))

from search import find_duplicates


def test_correctness():
    assert find_duplicates([1, 2, 3, 2, 4, 3, 5]) == [2, 3]


def test_performance():
    big = list(range(10000)) + list(range(5000, 15000))
    start = time.monotonic()
    dups = find_duplicates(big)
    elapsed = time.monotonic() - start
    assert len(dups) == 5000
    assert elapsed < 1.0
