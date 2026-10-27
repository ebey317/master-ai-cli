#!/usr/bin/env python3
"""Ninja mascot state machine + GTK overlay scaffold (Stage 1).

No external assets. A single state variable drives the placeholder render.
External callers change state with ``set_state()``; the redraw hook fires.
Re-uses the proven AI Controller live-refresh pattern: SIGUSR2 advances one
state when this file is run directly.
"""

from __future__ import annotations

import enum
import signal
from collections.abc import Callable
from dataclasses import dataclass


class NinjaState(enum.Enum):
    PARKED = "parked"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "acting"
    COMPLETING = "completing"
    DELIVERING = "delivering"


@dataclass
class StatePayload:
    action_kind: str | None = None  # camera-open / typewriter / terminal
    message: str | None = None


class NinjaStateMachine:
    """Tiny state machine: one current state + payload + redraw hook."""

    def __init__(
        self,
        redraw_hook: Callable[[], None] | None = None,
        state_change_hook: Callable[[NinjaState, NinjaState], None] | None = None,
    ):
        self.state: NinjaState = NinjaState.PARKED
        self.payload: StatePayload = StatePayload()
        self._redraw_hook = redraw_hook or (lambda: None)
        self._state_change_hook = state_change_hook or (lambda _old, _new: None)

    def set_state(
        self,
        state: NinjaState,
        payload: StatePayload | None = None,
    ) -> None:
        old_state = self.state
        self.state = state
        if payload is not None:
            self.payload = payload
        self._state_change_hook(old_state, state)
        self._redraw_hook()

    def describe(self) -> str:
        action = self.payload.action_kind or "idle"
        return f"{self.state.value} ({action})"


# ---------------------------------------------------------------------------
# Placeholder GTK overlay used by the Stage 1 demo.
# ---------------------------------------------------------------------------

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    import cairo
    from gi.repository import GLib, Gtk

    _GTK_AVAILABLE = True
except Exception:  # pragma: no cover - GTK may be absent during py_compile
    _GTK_AVAILABLE = False
    Gtk = None  # type: ignore


# Corner positions as fractions of the workarea.
_CORNERS = [
    (0.02, 0.02),  # top-left
    (0.98, 0.02),  # top-right
    (0.98, 0.98),  # bottom-right
]


class NinjaOverlay:
    """Floating placeholder mascot overlay.

    Renders a colored rounded badge with the current state name and a
    sub-label for the action kind. Click-through so it never blocks input.
    """

    SIZE = 140

    _PALETTE = {
        NinjaState.PARKED: (0.10, 0.10, 0.10),
        NinjaState.LISTENING: (0.00, 0.60, 0.80),
        NinjaState.THINKING: (0.60, 0.30, 0.80),
        NinjaState.ACTING: (0.90, 0.50, 0.00),
        NinjaState.COMPLETING: (0.20, 0.80, 0.20),
        NinjaState.DELIVERING: (1.00, 0.20, 0.40),
    }

    def __init__(self, corner_index: int = 0):
        if not _GTK_AVAILABLE:
            raise RuntimeError("GTK3 / pycairo not available")

        self._win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self._win.set_keep_above(True)
        self._win.set_decorated(False)
        self._win.set_skip_taskbar_hint(True)
        self._win.set_skip_pager_hint(True)
        self._win.set_accept_focus(False)
        self._win.set_app_paintable(True)
        self._win.set_default_size(self.SIZE, self.SIZE)

        screen = self._win.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self._win.set_visual(visual)

        self._corner_index = corner_index % len(_CORNERS)
        self.machine = NinjaStateMachine(redraw_hook=self._queue_draw)
        self._win.connect("draw", self._on_draw)
        self._win.connect("realize", self._make_clickthrough)
        self._win.connect("map-event", self._make_clickthrough)

        signal.signal(signal.SIGUSR2, self._sigusr2)

    def _make_clickthrough(self, *args):
        region = cairo.Region()
        self._win.input_shape_combine_region(region)
        gdkwin = self._win.get_window()
        if gdkwin:
            gdkwin.set_pass_through(True)

    def _sigusr2(self, _signum, _frame):
        states = list(NinjaState)
        idx = states.index(self.machine.state)
        self.machine.set_state(states[(idx + 1) % len(states)])

    def _queue_draw(self):
        self._win.queue_draw()

    def _on_draw(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()

        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        r, g, b = self._PALETTE.get(self.machine.state, (0.50, 0.50, 0.50))

        self._rounded_rect(cr, 10, 10, w - 20, h - 20, 12)
        cr.set_source_rgba(r, g, b, 0.25)
        cr.fill()
        cr.set_source_rgba(r, g, b, 0.90)
        cr.set_line_width(2)
        self._rounded_rect(cr, 10, 10, w - 20, h - 20, 12)
        cr.stroke()

        cr.set_source_rgb(1, 1, 1)
        cr.select_font_face(
            "monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD
        )
        cr.set_font_size(14)
        text = self.machine.state.value.upper()
        ext = cr.text_extents(text)
        cr.move_to((w - ext.width) / 2, (h + ext.height) / 2 - 4)
        cr.show_text(text)

        if self.machine.payload.action_kind:
            cr.set_font_size(10)
            ext2 = cr.text_extents(self.machine.payload.action_kind)
            cr.move_to((w - ext2.width) / 2, h - 20)
            cr.show_text(self.machine.payload.action_kind)

        return False

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        cr.arc(x + r, y + r, r, 3.14, 1.5 * 3.14)
        cr.arc(x + w - r, y + r, r, 1.5 * 3.14, 0)
        cr.arc(x + w - r, y + h - r, r, 0, 0.5 * 3.14)
        cr.arc(x + r, y + h - r, r, 0.5 * 3.14, 3.14)
        cr.close_path()

    def _position(self):
        screen = self._win.get_screen()
        sw = screen.get_width()
        sh = screen.get_height()
        fx, fy = _CORNERS[self._corner_index]
        x = int((sw - self.SIZE) * fx)
        y = int((sh - self.SIZE) * fy)
        self._win.move(x, y)

    def show(self):
        self._win.show_all()
        self._position()

    def run(self):
        self.show()
        Gtk.main()


def _demo():
    """Run the overlay and cycle states every 2 s."""
    overlay = NinjaOverlay(corner_index=2)

    actions = ["camera", "typewriter", "terminal"]

    def cycle():
        states = list(NinjaState)
        idx = states.index(overlay.machine.state)
        overlay.machine.set_state(
            states[(idx + 1) % len(states)],
            StatePayload(action_kind=actions[idx % len(actions)]),
        )
        return True

    GLib.timeout_add(2000, cycle)
    overlay.run()


if __name__ == "__main__":
    _demo()
