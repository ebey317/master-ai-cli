"""Smoke test for Sensei TUI focus-aware line scroll.

Run: python3 test_sensei_scroll.py
"""

import os
import sys

os.environ.setdefault("SENSEI_MOUSE", "1")
sys.path.insert(0, os.path.dirname(__file__))

from prompt_toolkit.output import DummyOutput

from sensei_tui import SenseiApp


def main():
    app = SenseiApp()
    app._app.output = DummyOutput()

    # Fill output so there is something to scroll
    for i in range(200):
        app.write(f"line {i:03d}\n")

    # Default focus is input; chat not focused
    assert app._chat_focused is False, "chat should start unfocused"

    # 2026-09-01: these used to be found by "no focus filter at all" - that
    # was the bug. An unconditional down/up binding matches even while the
    # input's completion menu is open and wins over _completion_down/
    # _completion_up (filtered to has_focus(input)), so completion-menu
    # navigation and input-history recall silently never fired. Reproduced
    # live: typing "/m" then pressing Down left complete_index at None
    # every time. Fix: chat-scroll down/up are now filtered to
    # self._chat_focused, same as every other chat-only binding, and this
    # test finds them the same way - then explicitly checks they get out
    # of the way of completion/history when input is focused.
    def _by_handler_name(name):
        for binding in app._app.key_bindings.bindings:
            if getattr(binding.handler, "__name__", "") == name:
                return binding
        return None

    global_up = _by_handler_name("_global_up")
    global_down = _by_handler_name("_global_down")
    completion_up = _by_handler_name("_completion_up")
    completion_down = _by_handler_name("_completion_down")
    escape_focus = next(
        (
            b
            for b in app._app.key_bindings.bindings
            if getattr(b, "keys", None) == ("escape",)
        ),
        None,
    )

    assert global_up is not None, "missing global Up binding"
    assert global_down is not None, "missing global Down binding"
    assert completion_up is not None, "missing _completion_up binding"
    assert completion_down is not None, "missing _completion_down binding"
    assert escape_focus is not None, "missing Escape binding"

    # Input focused (default state): chat-scroll bindings must NOT match,
    # so Down/Up actually reach _completion_down/_completion_up instead of
    # being silently swallowed - this is the regression itself. Checked via
    # self._chat_focused directly (no get_app() involved, so it's reliable
    # outside a running event loop).
    assert global_up.filter() is False, (
        "global Up must not match while input is focused"
    )
    assert global_down.filter() is False, (
        "global Down must not match while input is focused"
    )
    # completion_up/completion_down are filtered with has_focus(self._input),
    # which calls get_app() internally - that only resolves correctly inside
    # a real running Application (has_focus() needs the live layout's focus
    # stack, which isn't reliably populated by the constructor outside
    # run_async()). Not asserted here for that reason; the actual regression
    # this file exists to catch - real Down-arrow key dispatch reaching
    # complete_next() instead of being swallowed - is verified separately by
    # piping real keystrokes into a running app instance (done for this fix,
    # 2026-09-01: confirmed complete_index advances 0->1 on two real Down
    # presses after "/", which failed with index staying None before the fix).

    # Focus chat and exercise the handlers directly
    app._focus_chat()
    assert global_up.filter() is True, "global Up must match once chat is focused"
    assert global_down.filter() is True, "global Down must match once chat is focused"
    assert app._chat_focused is True, "focus_chat should set flag"
    assert app._app.layout.current_window == app._output_window, (
        "layout focus should be output window"
    )

    event = type("E", (), {"app": app._app})
    before = app._scroll_offset
    # 2026-10-21 smooth scroll: handlers set a TARGET; an animator glides the
    # visible offset there (Elijah: old per-event teleports felt like
    # "ticks"). So the handler's immediate contract is target movement, and
    # the visible offset equals the target once the glide settles.
    global_up.handler(event)
    assert app._scroll_target == before + 1, (
        f"global Up should scroll chat (target): {app._scroll_target}"
    )
    assert app._wait_scroll_settled(2.0), "scroll glide did not settle"
    assert app._scroll_offset == before + 1, (
        f"global Up should scroll chat (glided): {app._scroll_offset}"
    )
    global_down.handler(event)
    assert app._wait_scroll_settled(2.0), "scroll glide did not settle"
    assert app._scroll_offset == before, (
        f"global Down should un-scroll chat: {app._scroll_offset}"
    )

    # Focus input; the filter (not the handler body) is what keeps chat-
    # scroll out of the way now, so real key dispatch never invokes these
    # handlers here - assert that via the filter, not a direct call
    # (calling .handler() directly bypasses the filter prompt_toolkit
    # itself would consult, so it can't prove real-usage behavior).
    app._focus_input()
    assert app._chat_focused is False, "focus_input should clear flag"
    assert app._app.layout.current_window == app._input.window, (
        "layout focus should be input"
    )
    assert global_up.filter() is False, (
        "global Up must not match once input is refocused"
    )
    assert global_down.filter() is False, (
        "global Down must not match once input is refocused"
    )

    # Scroll public API still works (target + glide settle)
    app.scroll("up", n=5)
    assert app._scroll_target == 5, f"scroll up target failed: {app._scroll_target}"
    assert app._wait_scroll_settled(2.0), "scroll glide did not settle"
    assert app._scroll_offset == 5, f"scroll up failed: {app._scroll_offset}"
    app.scroll("down", n=2)
    assert app._wait_scroll_settled(2.0), "scroll glide did not settle"
    assert app._scroll_offset == 3, f"scroll down failed: {app._scroll_offset}"
    app.scroll("bottom")
    assert app._wait_scroll_settled(2.0), "scroll glide did not settle"
    assert app._scroll_offset == 0, "scroll bottom failed"

    # 2026-10-21 easing kernel: pure-function guarantees
    from sensei_tui import _eased_scroll_step

    disp = 0.0
    frames = 0
    while disp != 100.0:
        delta = 100.0 - disp
        new = _eased_scroll_step(disp, 100.0, 1 / 60)
        progress = new - disp
        if delta <= 0.75:
            # snap window: the frame lands EXACTLY on target
            assert abs(progress - delta) < 1e-9, f"missed snap: {progress} vs {delta}"
        else:
            # never stalls: progress ≥ min_step (0.5) while far from target
            assert progress >= 0.49, f"stall at disp={disp}: step={progress}"
        assert new > disp, "non-monotonic glide"
        disp = new
        frames += 1
        assert frames < 1000, "easing failed to converge"
    assert disp == 100.0

    # glide up from 0: monotonic convergence + never negative
    disp = 0.0
    frames = 0
    while disp != 100.0:
        disp = _eased_scroll_step(disp, 100.0, 1 / 60)
        assert disp >= 0.0, "went negative"
        frames += 1
        assert frames < 1000, "easing failed to converge"
    disp = 50.0
    while abs(0.0 - disp) > 0:
        new = _eased_scroll_step(disp, 0.0, 1 / 60)
        assert new <= disp, f"non-monotonic decay at {disp}"
        assert new >= 0.0, f"clamped below 0: {new}"
        disp = new
        frames += 1
        assert frames < 2000, "easing failed to converge"

    # dt hiccup clamp: an absurd dt behaves exactly like 0.1 s
    assert _eased_scroll_step(0.0, 50.0, 1e6) == _eased_scroll_step(0.0, 50.0, 0.1)

    # ── 2026-10-21 v2: vertical_scroll force-apply + in-viewport cursor ──
    # ptk auto-scroll only keeps the invisible cursor VISIBLE — it never
    # glides, so wheel notches moved nothing until cursors crossed viewport
    # edges. The animator now forces vertical_scroll per frame; these
    # assertions pin the math (viewport = [vtotal - h - off, vtotal - off))
    # and the safe no-op when render_info is absent (never rendered).
    from types import SimpleNamespace

    real_window = app._output_window  # restored at the very end of v2
    app._output_window = SimpleNamespace(
        render_info=SimpleNamespace(vertical_scroll=48, window_height=12)
    )
    app._output_render_lines = 60
    app._apply_scroll_to_render(9)
    assert app._output_window.render_info.vertical_scroll == 39, "glide apply failed"
    app._apply_scroll_to_render(0)
    assert app._output_window.render_info.vertical_scroll == 48, "return to bottom failed"
    app._apply_scroll_to_render(1000)  # clamp at the top of the buffer
    assert app._output_window.render_info.vertical_scroll == 0, "top clamp failed"
    app._output_window.render_info.vertical_scroll = 39
    app._apply_scroll_to_render(9)  # idempotent no-op must not re-invalidate
    assert app._output_window.render_info.vertical_scroll == 39, "idempotence failed"

    # No render yet (render_info=None) → helper must be a silent no-op
    app._output_window = SimpleNamespace(render_info=None)
    app._apply_scroll_to_render(3)  # must not raise

    # Empty buffer → silent no-op
    app._output_window = SimpleNamespace(
        render_info=SimpleNamespace(vertical_scroll=0, window_height=12)
    )
    app._output_render_lines = 0
    app._apply_scroll_to_render(5)
    assert app._output_window.render_info.vertical_scroll == 0, "empty-buffer no-op failed"

    # ── cursor math against a REAL 60-line buffer (cache warms here) ──
    # NOTE: _output_render_lines is derived from the rendered cache, not an
    # independent pin — swapping the number manually never affects the next
    # render; drive it through write()/clear_output() (the render cache is
    # keyed to _output_version, so new writes always re-warm it correctly).
    app.clear_output()
    for i in range(60):
        app.write(f"row {i:03d}\n")
    app._scroll_offset = 0  # bottom (also converges the animator)
    assert app._wait_scroll_settled(2.0), "reset glide did not settle"
    assert app._get_output_cursor().y == 59, (
        f"cursor should sit on the last visible row: {app._get_output_cursor().y}"
    )

    # Cursor stays INSIDE the viewport so Window auto-scroll never fights
    # the glide: cursor y = total - offset - 1 (last visible row).
    app._scroll_offset = 20  # setter drives the animator; also converges
    assert app._wait_scroll_settled(2.0), "setter glide did not settle"
    assert app._scroll_offset == 20, f"setter offset failed: {app._scroll_offset}"
    assert app._get_output_cursor().y == 39, "cursor not inside viewport after setter"
    # The animator's force-apply lands within one 60fps frame after converge;
    # poll for it (disp==target flips BEFORE the apply finishes — race-safe).
    import time as _time
    _deadline = _time.time() + 2.0
    while app._output_window.render_info.vertical_scroll != 28 and _time.time() < _deadline:
        _time.sleep(0.01)
    assert app._output_window.render_info.vertical_scroll == 28, (
        f"animator must force vscroll: {app._output_window.render_info.vertical_scroll}"
    )
    app._output_window = real_window

    print("PASS: focus-aware chat scroll is wired correctly")


if __name__ == "__main__":
    main()
