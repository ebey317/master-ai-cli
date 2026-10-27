#!/usr/bin/env python3
"""Thootie — Master AI desktop companion (avatar window + states).

Stage 2: the eight Thootie states, spec corners with the notification
corner excluded, idle drift along the waypoint path, and an attention
pulse for incoming approvals. The approval card lives in
``ninja.card_box``; the read-only queue poll in ``ninja.queue_watch``.

Display contract (operator spec 2026-10-26/27): small always (~220px),
never modal, never full-screen, notification corner stays clear.
"""

from __future__ import annotations

import enum
import signal
from collections.abc import Callable
from dataclasses import dataclass

from scripts.theme import Theme, hex_to_rgb, load_theme


class ThootieState(enum.Enum):
    IDLE = "idle"
    WORKING = "working"
    SORTING = "sorting"  # covers the spec's SORTING/THINKING
    APPROVAL_INCOMING = "approval-incoming"
    APPROVAL_PRESENTED = "approval-presented"
    APPROVED_RUN = "approved-run"
    DENIED = "denied"
    EDIT = "edit"


@dataclass
class StatePayload:
    action_kind: str | None = None
    message: str | None = None


class ThootieStateMachine:
    """Tiny state machine: one current state + payload + redraw hook."""

    def __init__(
        self,
        redraw_hook: Callable[[], None] | None = None,
        state_change_hook: Callable[[ThootieState, ThootieState], None] | None = None,
    ):
        self.state: ThootieState = ThootieState.IDLE
        self.payload: StatePayload = StatePayload()
        self._redraw_hook = redraw_hook or (lambda: None)
        self._state_change_hook = state_change_hook or (lambda _o, _n: None)

    def set_state(
        self,
        state: ThootieState,
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


def drift_step(
    current: tuple[float, float],
    target: tuple[float, float],
    alpha: float = 0.08,
) -> tuple[float, float]:
    """Ease ``current`` toward ``target``; ``alpha`` in (0, 1]."""
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    return (
        current[0] + (target[0] - current[0]) * alpha,
        current[1] + (target[1] - current[1]) * alpha,
    )


# ---------------------------------------------------------------------------
# GTK overlay: floating placeholder avatar.
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


# Waypoint path as fractions of the screen: top-left -> bottom-left ->
# bottom-right. Top-right is the notification corner: exclusion zone,
# the avatar never parks there.
_WAYPOINTS = [
    (0.02, 0.02),  # top-left
    (0.02, 0.98),  # bottom-left
    (0.98, 0.98),  # bottom-right
]
_EXCLUDED_NOTIFICATION_CORNER = (0.98, 0.02)  # top-right


class ThootieOverlay:
    """Floating placeholder avatar overlay.

    Renders a colored rounded badge with the current state name and a
    sub-label for the action kind. Click-through so it never blocks input.
    """

    SIZE = 220

    _PALETTE = {
        ThootieState.IDLE: "accent",
        ThootieState.WORKING: "accent",
        ThootieState.SORTING: "accent",
        ThootieState.APPROVAL_INCOMING: "accent",
        ThootieState.APPROVAL_PRESENTED: "accent",
        ThootieState.APPROVED_RUN: "pin",
        ThootieState.DENIED: "text_secondary",
        ThootieState.EDIT: "accent",
    }

    def __init__(self, waypoint_index: int = 0, theme: Theme | None = None):
        if not _GTK_AVAILABLE:
            raise RuntimeError("GTK3 / pycairo not available")

        self._theme = theme or load_theme()

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

        self._waypoint_index = waypoint_index % len(_WAYPOINTS)
        self._target_index = (self._waypoint_index + 1) % len(_WAYPOINTS)
        self._pos: list[float] = [0.0, 0.0]
        self._pulse_on = False
        self.machine = ThootieStateMachine(redraw_hook=self._queue_draw)
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
        states = list(ThootieState)
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

        field = self._PALETTE.get(self.machine.state, "accent")
        r, g, b = hex_to_rgb(getattr(self._theme, field))
        pulsing = (
            self.machine.state == ThootieState.APPROVAL_INCOMING and self._pulse_on
        )
        fill_alpha = 0.55 if pulsing else 0.25

        self._rounded_rect(cr, 10, 10, w - 20, h - 20, 16)
        cr.set_source_rgba(r, g, b, fill_alpha)
        cr.fill()
        cr.set_source_rgba(r, g, b, 0.90)
        cr.set_line_width(3 if pulsing else 2)
        self._rounded_rect(cr, 10, 10, w - 20, h - 20, 16)
        cr.stroke()

        tr, tg, tb = hex_to_rgb(self._theme.text)
        cr.set_source_rgb(tr, tg, tb)
        cr.select_font_face(
            "monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD
        )
        cr.set_font_size(16)
        text = self.machine.state.value.upper()
        ext = cr.text_extents(text)
        cr.move_to((w - ext.width) / 2, (h + ext.height) / 2 - 4)
        cr.show_text(text)

        if self.machine.payload.action_kind:
            cr.set_font_size(11)
            ext2 = cr.text_extents(self.machine.payload.action_kind)
            cr.move_to((w - ext2.width) / 2, h - 24)
            cr.show_text(self.machine.payload.action_kind)

        return False

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        cr.arc(x + r, y + r, r, 3.14, 1.5 * 3.14)
        cr.arc(x + w - r, y + r, r, 1.5 * 3.14, 0)
        cr.arc(x + w - r, y + h - r, r, 0, 0.5 * 3.14)
        cr.arc(x + r, y + h - r, r, 0.5 * 3.14, 3.14)
        cr.close_path()

    def _waypoint_px(self, index: int) -> tuple[int, int]:
        screen = self._win.get_screen()
        sw = screen.get_width()
        sh = screen.get_height()
        fx, fy = _WAYPOINTS[index % len(_WAYPOINTS)]
        return (int((sw - self.SIZE) * fx), int((sh - self.SIZE) * fy))

    def _drift_tick(self):
        tx, ty = self._waypoint_px(self._target_index)
        nx, ny = drift_step((self._pos[0], self._pos[1]), (tx, ty))
        self._pos = [nx, ny]
        self._win.move(int(nx), int(ny))
        if abs(nx - tx) < 2 and abs(ny - ty) < 2:
            self._target_index = (self._target_index + 1) % len(_WAYPOINTS)
        if self.machine.state == ThootieState.APPROVAL_INCOMING:
            self._pulse_on = not self._pulse_on
            self._win.queue_draw()
        return True

    def show(self):
        x, y = self._waypoint_px(self._waypoint_index)
        self._pos = [float(x), float(y)]
        self._win.show_all()
        self._win.move(x, y)
        GLib.timeout_add(50, self._drift_tick)

    def run(self):
        self.show()
        Gtk.main()


def _demo():
    """Run the overlay and cycle states every 2 s."""
    overlay = ThootieOverlay(waypoint_index=0)

    actions = ["camera", "typewriter", "terminal"]

    def cycle():
        states = list(ThootieState)
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
