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

    # Find the global Up/Down bindings (last registered, no focus filter).
    def _is_unfiltered(binding):
        f = binding.filter
        try:
            return f is None or f() is True
        except Exception:
            return False

    global_up = None
    global_down = None
    for binding in app._app.key_bindings.bindings:
        keys = getattr(binding, "keys", None)
        if keys == ("up",) and _is_unfiltered(binding):
            global_up = binding
        elif keys == ("down",) and _is_unfiltered(binding):
            global_down = binding

    escape_focus = next((b for b in app._app.key_bindings.bindings
                         if getattr(b, "keys", None) == ("escape",)), None)

    assert global_up is not None, "missing global Up binding"
    assert global_down is not None, "missing global Down binding"
    assert escape_focus is not None, "missing Escape binding"

    # Global bindings always match; they internally check _chat_focused.
    assert global_up.filter() is True, "global Up should always be active"
    assert global_down.filter() is True, "global Down should always be active"

    # Focus chat and exercise the handlers directly
    app._focus_chat()
    assert app._chat_focused is True, "focus_chat should set flag"
    assert app._app.layout.current_window == app._output_window, "layout focus should be output window"

    event = type("E", (), {"app": app._app})
    before = app._scroll_offset
    global_up.handler(event)
    assert app._scroll_offset == before + 1, f"global Up should scroll chat: {app._scroll_offset}"
    global_down.handler(event)
    assert app._scroll_offset == before, f"global Down should un-scroll chat: {app._scroll_offset}"

    # Focus input; global handlers should no-op
    app._focus_input()
    assert app._chat_focused is False, "focus_input should clear flag"
    assert app._app.layout.current_window == app._input.window, "layout focus should be input"
    before = app._scroll_offset
    global_up.handler(event)
    global_down.handler(event)
    assert app._scroll_offset == before, f"Up/Down should not scroll when input focused: {app._scroll_offset}"

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
