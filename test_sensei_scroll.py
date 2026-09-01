"""Smoke test for Sensei TUI focus-aware line scroll.

Run: python3 test_sensei_scroll.py
"""
import os
import sys

os.environ.setdefault("SENSEI_MOUSE", "1")
sys.path.insert(0, os.path.dirname(__file__))

from sensei_tui import SenseiApp
from prompt_toolkit.output import DummyOutput


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
    escape_focus = next((b for b in app._app.key_bindings.bindings
                         if getattr(b, "keys", None) == ("escape",)), None)

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
    assert global_up.filter() is False, "global Up must not match while input is focused"
    assert global_down.filter() is False, "global Down must not match while input is focused"
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
    assert app._app.layout.current_window == app._output_window, "layout focus should be output window"

    event = type("E", (), {"app": app._app})
    before = app._scroll_offset
    global_up.handler(event)
    assert app._scroll_offset == before + 1, f"global Up should scroll chat: {app._scroll_offset}"
    global_down.handler(event)
    assert app._scroll_offset == before, f"global Down should un-scroll chat: {app._scroll_offset}"

    # Focus input; the filter (not the handler body) is what keeps chat-
    # scroll out of the way now, so real key dispatch never invokes these
    # handlers here - assert that via the filter, not a direct call
    # (calling .handler() directly bypasses the filter prompt_toolkit
    # itself would consult, so it can't prove real-usage behavior).
    app._focus_input()
    assert app._chat_focused is False, "focus_input should clear flag"
    assert app._app.layout.current_window == app._input.window, "layout focus should be input"
    assert global_up.filter() is False, "global Up must not match once input is refocused"
    assert global_down.filter() is False, "global Down must not match once input is refocused"

    # Scroll public API still works
    app.scroll("up", n=5)
    assert app._scroll_offset == 5, f"scroll up failed: {app._scroll_offset}"
    app.scroll("down", n=2)
    assert app._scroll_offset == 3, f"scroll down failed: {app._scroll_offset}"
    app.scroll("bottom")
    assert app._scroll_offset == 0, "scroll bottom failed"

    print("PASS: focus-aware chat scroll is wired correctly")


if __name__ == "__main__":
    main()
