"""Bounded chat-buffer tests (2026-10-22 RAM-leak fix).

Run: python3 test_sensei_buffer.py
"""

import os
import sys

os.environ.setdefault("SENSEI_MOUSE", "1")
sys.path.insert(0, os.path.dirname(__file__))

from sensei_tui import SenseiApp


def main():
    from sensei_tui import (
        SenseiApp,
        _OUTPUT_BUFFER_CAP_BYTES,
        _OUTPUT_BUFFER_MARKER,
        _OUTPUT_BUFFER_TRIM_TO,
    )

    app = SenseiApp()

    # Small session: no trim, bytes counter tracks the buffer exactly
    app.write("hello\n")
    app.write("world\n")
    assert app._output_bytes == len("hello\n") + len("world\n"), app._output_bytes
    assert "hello\n" in "".join(app._output_chunks)
    assert _OUTPUT_BUFFER_MARKER not in "".join(app._output_chunks), "premature trim"

    # Force the cap: 100 heavy chunks (≈4.9MB) — the buffer must self-trim to
    # ≤ cap. Deliberately a bounded `for` (a `while bytes <= cap` loop can
    # never exit once trimming keeps the counter below the cap — that was
    # the false "hang" while first running this suite).
    for n in range(100):
        app.write(f"chunk {n:06d} " + "x" * 49_000 + "\n")
    joined = "".join(app._output_chunks)

    # Bounded; newest data survived; marker sits exactly once (older trims'
    # markers are themselves dropped as oldest data)
    assert app._output_bytes <= _OUTPUT_BUFFER_CAP_BYTES, app._output_bytes
    assert len(joined) == app._output_bytes, (
        f"joined {len(joined)} vs bytes {app._output_bytes}"
    )
    assert joined.startswith(_OUTPUT_BUFFER_MARKER), "trim marker must be first"
    assert joined.count(_OUTPUT_BUFFER_MARKER) == 1, "exactly one marker"
    last_line = f"chunk {n:06d} "
    assert last_line in joined, "the newest chunk must be kept"
    assert "chunk 000000 " not in joined, "oldest chunk must be trimmed"

    # Scroll-offset semantics while trimmed: offset counts above the BOTTOM;
    # the bottom tail is preserved, so a scrolled-up user keeps their place.
    app._scroll_offset = 3
    before = app._scroll_offset
    app.write("tail-keeper after trim\n")
    assert app._scroll_offset == before + 1, "follow must still add lines after trim"
    assert "tail-keeper after trim\n" in "".join(app._output_chunks[-2:]), (
        "newest write must be at the buffer tail"
    )
    assert _OUTPUT_BUFFER_MARKER not in app._output_chunks[-1], "marker must not repeat"

    # clear_output resets the counter
    app.clear_output()
    assert app._output_bytes == 0, app._output_bytes
    assert app._output_chunks == [], app._output_chunks

    print("PASS: bounded chat buffer (cap, trim, marker, follow, reset)")


if __name__ == "__main__":
    main()