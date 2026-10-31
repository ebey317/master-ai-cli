#!/usr/bin/env python3
"""Thootie — Master AI desktop companion (avatar window + states).

Floating professional avatar: perpetual gentle hover, approval cards spawn
from the avatar (same window family, anchored beside it), living animation
variety, crisp PNG polish with theme-palette only.
"""

from __future__ import annotations

import enum
import math
import os
import random
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

from scripts.theme import Theme, hex_to_rgb, load_theme  # noqa: E402


class ThootieState(enum.Enum):
    IDLE = "idle"
    WORKING = "working"
    SORTING = "sorting"
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
        self, state: ThootieState, payload: StatePayload | None = None
    ) -> None:
        old = self.state
        self.state = state
        if payload is not None:
            self.payload = payload
        self._state_change_hook(old, state)
        self._redraw_hook()

    def describe(self) -> str:
        action = self.payload.action_kind or "idle"
        return f"{self.state.value} ({action})"


def drift_step(
    current: tuple[float, float],
    target: tuple[float, float],
    alpha: float = 0.04,
) -> tuple[float, float]:
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    return (
        current[0] + (target[0] - current[0]) * alpha,
        current[1] + (target[1] - current[1]) * alpha,
    )


class LivingIdle:
    """Calm perpetual hover: single authority, capped displacement, slow tick.
    Returns (bob_x, bob_y, tilt, scale) each tick. ONE motion source for
    overlay: bob+drift composed here, capped per tick, halved amplitudes.
    """

    TICK_S = 0.12
    MAX_BOB_DISP_PER_TICK = 1.2

    def __init__(self, tick_s: float = 0.12) -> None:
        self.tick_s = tick_s
        self.bob_x = 0.0
        self.bob_y = 0.0
        self.bob_target_x = 0.0
        self.bob_target_y = 0.0
        self.bob_amp_x = 1.0
        self.bob_amp_y = 1.6
        self.phase_x = random.random() * math.tau
        self.phase_y = random.random() * math.tau
        self.phase_z = random.random() * math.tau
        self.period_x = 6.5
        self.period_y = 7.2
        self.tilt = 0.0
        self.tilt_target = 0.0
        self.scale = 1.0
        self.scale_target = 1.0
        self.reseed_ticks = 0
        self.micro_ticks = 0
        self._last_bob_x = 0.0
        self._last_bob_y = 0.0

    def _reseed(self) -> None:
        self.bob_amp_x = random.uniform(0.5, 2.2)
        self.bob_amp_y = random.uniform(0.8, 3.2)
        self.period_x = random.uniform(5.0, 12.0)
        self.period_y = random.uniform(5.5, 13.0)
        self.phase_x += random.uniform(-0.25, 0.25)
        self.phase_y += random.uniform(-0.25, 0.25)
        self.phase_z = self.phase_z * 0.85 + random.random() * math.tau * 0.15
        self.reseed_ticks = random.randint(60, 180)
        self.bob_target_x = random.uniform(-self.bob_amp_x, self.bob_amp_x) * 0.6
        self.bob_target_y = random.uniform(-self.bob_amp_y, self.bob_amp_y) * 0.6
        self.tilt_target = random.uniform(-0.04, 0.04)
        self.scale_target = random.uniform(0.985, 1.025)

    def tick(self) -> tuple[float, float, float, float]:
        if self.reseed_ticks <= 0:
            self._reseed()
        else:
            self.reseed_ticks -= 1

        wx = 2 * math.pi / self.period_x
        wy = 2 * math.pi / self.period_y
        self.phase_x += wx * self.tick_s
        self.phase_y += wy * self.tick_s
        self.phase_z += (2 * math.pi / 14.0) * self.tick_s

        sine_x = math.sin(self.phase_x) * self.bob_amp_x * 0.25
        sine_y = math.sin(self.phase_y) * self.bob_amp_y * 0.25
        sine_x += 0.10 * math.sin(self.phase_x * 0.37 + self.phase_z) * self.bob_amp_x
        sine_y += (
            0.10 * math.sin(self.phase_y * 0.31 + self.phase_z * 0.67) * self.bob_amp_y
        )

        if random.random() < 0.05:
            self.bob_target_x += random.uniform(-0.3, 0.3)
            self.bob_target_x = max(
                -self.bob_amp_x, min(self.bob_amp_x, self.bob_target_x)
            )
        if random.random() < 0.05:
            self.bob_target_y += random.uniform(-0.3, 0.3)
            self.bob_target_y = max(
                -self.bob_amp_y, min(self.bob_amp_y, self.bob_target_y)
            )
        if random.random() < 0.02:
            self.bob_target_x = random.uniform(-self.bob_amp_x, self.bob_amp_x) * 0.5
        if random.random() < 0.02:
            self.bob_target_y = random.uniform(-self.bob_amp_y, self.bob_amp_y) * 0.5

        jitter_x = random.uniform(-0.14, 0.14)
        jitter_y = random.uniform(-0.16, 0.16)
        tx = 0.30 * sine_x + 0.70 * self.bob_target_x + jitter_x
        ty = 0.30 * sine_y + 0.70 * self.bob_target_y + jitter_y

        self.bob_x = drift_step((self.bob_x, 0), (tx, 0), 0.18)[0]
        self.bob_y = drift_step((self.bob_y, 0), (ty, 0), 0.16)[0]

        dx = self.bob_x - self._last_bob_x
        dy = self.bob_y - self._last_bob_y
        if abs(dx) > self.MAX_BOB_DISP_PER_TICK:
            self.bob_x = self._last_bob_x + math.copysign(
                self.MAX_BOB_DISP_PER_TICK, dx
            )
        if abs(dy) > self.MAX_BOB_DISP_PER_TICK:
            self.bob_y = self._last_bob_y + math.copysign(
                self.MAX_BOB_DISP_PER_TICK, dy
            )
        self._last_bob_x, self._last_bob_y = self.bob_x, self.bob_y

        if random.random() < 0.03:
            self.tilt_target = random.uniform(-0.05, 0.05)
        self.tilt = drift_step((self.tilt, 0), (self.tilt_target, 0), 0.06)[0]

        if random.random() < 0.02:
            self.scale_target = random.uniform(0.985, 1.03)
        self.scale = drift_step((self.scale, 0), (self.scale_target, 0), 0.05)[0]

        if abs(self.bob_x) < 0.06 and abs(self.bob_y) < 0.06:
            self.bob_x += random.uniform(-0.15, 0.15)
            self.bob_y += random.uniform(-0.15, 0.15)

        return (self.bob_x, self.bob_y, self.tilt, self.scale)


# ---------------------------------------------------------------------------
# GTK overlay — floating professional avatar
# ---------------------------------------------------------------------------

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    import cairo as _cairo
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

    _GTK_AVAILABLE = True
except Exception:  # pragma: no cover
    _GTK_AVAILABLE = False
    Gtk = None  # type: ignore
    Gdk = None  # type: ignore
    GdkPixbuf = None  # type: ignore
    _cairo = None  # type: ignore


# Patrol killed: avatar parks near corner, only gentle hover. No large drifts.
_WAYPOINTS = [
    (0.02, 0.02),  # top-left (owner preference)
    (0.02, 0.92),  # bottom-left
    (0.88, 0.92),  # bottom-right (pulled in so ACTIVE fits)
]
PATROL_ENABLED = False
PATROL_ALPHA = 0.0  # disabled
_EXCLUDED_NOTIFICATION_CORNER = (0.98, 0.02)

_AVATAR_PATHS: dict[str, Path] = {
    "ninja": Path.home() / "master-ai-cli" / "ninja" / "assets" / "ninja_avatar.png",
    "tahuti": Path.home() / "projects" / "tahuti" / "assets" / "tahuti_avatar.png",
    "viking": Path.home() / "master-ai-cli" / "ninja" / "assets" / "viking_avatar.png",
}


def _avatar_path(agent: str) -> Path | None:
    path = _AVATAR_PATHS.get(agent)
    if path and path.exists():
        return path
    for p in _AVATAR_PATHS.values():
        if p.exists():
            return p
    return None


def _ensure_display() -> None:
    if _GTK_AVAILABLE and "DISPLAY" not in os.environ:
        try:
            display = Gdk.Display.get_default()  # type: ignore[union-attr]
        except Exception:
            display = None
        if display is None:
            os.environ["DISPLAY"] = ":0"


def _build_css(theme: Theme) -> bytes:
    return f"""
window {{
    background-color: transparent;
}}
#panel {{
    background-color: transparent;
    border: none;
    padding: 0;
}}
#avatar-area {{
    background-color: transparent;
}}
#title {{
    color: {theme.accent};
    font-family: monospace;
    font-size: 13px;
    font-weight: bold;
    margin-bottom: 4px;
}}
#state {{
    color: {theme.text_secondary};
    font-family: monospace;
    font-size: 10px;
    margin-top: 2px;
}}
#card {{
    color: {theme.text};
    font-family: monospace;
    font-size: 12px;
}}
#card label {{
    color: {theme.text_secondary};
}}
button {{
    background-color: {theme.surface};
    color: {theme.text};
    border: 1px solid {theme.border};
    border-radius: 6px;
    font-family: monospace;
    font-size: 12px;
    padding: 6px 10px;
    margin: 3px;
}}
button:hover {{
    background-color: {theme.border};
}}
button.selected {{
    background-color: {theme.accent};
    color: {theme.background};
    border-color: {theme.accent};
}}
button.success {{
    background-color: {theme.pin};
    color: {theme.background};
    border-color: {theme.pin};
}}
""".encode()


_OPAQUE_CSS_TAIL = """
window {{
    background-color: {bg};
    border: 2px solid {accent};
    border-radius: 10px;
}}
#panel {{
    background-color: {bg};
    border: none;
    padding: 6px;
}}
"""


def _build_css_active(theme: Theme) -> bytes:
    base = _build_css(theme).decode("utf-8")
    tail = _OPAQUE_CSS_TAIL.format(bg=theme.background, accent=theme.accent)
    return (base + tail).encode("utf-8")


class ThootieOverlay:
    """Floating professional avatar — perpetual hover, spawn-from-avatar cards."""

    IDLE_SIZE = 128
    AVATAR_SIZE = 108
    ACTIVE_WIDTH = 420
    ACTIVE_HEIGHT = 280

    SHAKE_AMP = 2
    SHAKE_DURATION = 15
    FLOURISH_DURATION = 25
    FLOURISH_SPIN = 0.0
    SPAWN_DURATION = 10
    MAX_DISP_PER_TICK = 3.0  # total window move cap per tick (few px)
    TICK_MS = 120

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

    def __init__(
        self, waypoint_index: int = 0, theme: Theme | None = None, agent: str = "ninja"
    ):
        if not _GTK_AVAILABLE:
            raise RuntimeError("GTK3 not available")
        _ensure_display()
        if Gdk.Display.get_default() is None:  # type: ignore[union-attr]
            raise RuntimeError("no GTK display available")

        self._theme = theme or load_theme()
        self._agent = agent
        self._base_pixbuf: GdkPixbuf.Pixbuf | None = None  # type: ignore[valid-type]

        self._win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self._win.set_keep_above(True)
        self._win.set_decorated(False)
        self._win.set_skip_taskbar_hint(True)
        self._win.set_skip_pager_hint(True)
        self._win.set_accept_focus(False)
        self._win.set_default_size(self.IDLE_SIZE, self.IDLE_SIZE)

        screen = self._win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self._win.set_visual(visual)
        self._win.set_app_paintable(True)
        # Draggable card (2026-10-31): operator moves it like the slide
        # keyboard — POPUP windows have no title bar. Presses on buttons
        # are consumed by the buttons first, so clicks still work.
        self._drag = None
        self._win.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self._win.connect("button-press-event", self._on_drag_begin)
        self._win.connect("motion-notify-event", self._on_drag_motion)
        self._win.connect("button-release-event", self._on_drag_end)
        self._css_provider = Gtk.CssProvider()
        self._css_provider.load_from_data(_build_css(self._theme))
        Gtk.StyleContext.add_provider_for_screen(
            screen, self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._panel = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._panel.set_name("panel")
        self._win.add(self._panel)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        left.set_size_request(self.IDLE_SIZE, self.IDLE_SIZE)
        left.set_halign(Gtk.Align.CENTER)
        self._avatar = self._make_avatar_area()
        left.pack_start(self._avatar, True, True, 0)

        self._agent_label = Gtk.Label(label=self._agent.upper())
        self._agent_label.set_name("title")
        left.pack_start(self._agent_label, False, False, 0)

        self._state_label = Gtk.Label(label=ThootieState.IDLE.value.upper())
        self._state_label.set_name("state")
        left.pack_start(self._state_label, False, False, 0)

        self._panel.pack_start(left, False, False, 0)

        # Right: card spawned FROM avatar — same window family, anchored beside
        self._right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._right.set_no_show_all(True)

        self._title_label = Gtk.Label(label=f"{self._agent.upper()} RUN CARD")
        self._title_label.set_name("title")
        self._title_label.set_xalign(0.0)
        self._right.pack_start(self._title_label, False, False, 0)

        self._card_label = Gtk.Label(label="")
        self._card_label.set_name("card")
        self._card_label.set_xalign(0.0)
        self._card_label.set_yalign(0.0)
        self._card_label.set_line_wrap(True)
        self._card_label.set_line_wrap_mode(2)
        self._right.pack_start(self._card_label, True, True, 0)

        self._button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._button_box.set_halign(Gtk.Align.CENTER)
        self._button_box.set_valign(Gtk.Align.END)
        self._button_box.set_homogeneous(True)
        self._button_box.set_no_show_all(True)
        self._right.pack_start(self._button_box, False, False, 0)

        self._panel.pack_start(self._right, True, True, 0)

        self._waypoint_index = waypoint_index % len(_WAYPOINTS)
        self._target_index = self._waypoint_index
        self._pos: list[float] = [0.0, 0.0]
        self._pulse_on = False
        self._clickthrough = True
        self._active = False
        self._approval_verdict: str | None = None
        self._approval_loop: Any | None = None

        self._living_idle = LivingIdle(tick_s=0.12)
        self._shake_phase = 0.0
        self._avatar_scale = 1.0
        self._avatar_angle = 0.0
        self._shake_remaining = 0
        self._flourish_remaining = 0
        self._spawn_remaining = 0
        self._spawn_progress = 1.0
        self._state_pulse = 0.0  # per-state time accumulator

        self.machine = ThootieStateMachine(
            redraw_hook=self._on_state_change,
            state_change_hook=self._on_state_change,
        )
        self._win.connect("realize", self._make_clickthrough)
        self._win.connect("map-event", self._make_clickthrough)

        signal.signal(signal.SIGUSR2, self._sigusr2)

        self._set_active(False)

    def _make_avatar_area(self) -> Any:
        area = Gtk.DrawingArea()
        area.set_size_request(self.AVATAR_SIZE, self.AVATAR_SIZE)
        area.set_name("avatar-area")
        area.connect("draw", self._draw_avatar)
        return area

    def _load_base_pixbuf(self) -> GdkPixbuf.Pixbuf | None:
        path = _avatar_path(self._agent)
        if not path:
            return None
        try:
            # Crisp scaling: HYPER is highest quality for downscale
            interp = GdkPixbuf.InterpType.HYPER
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(path), self.AVATAR_SIZE, self.AVATAR_SIZE, True
            )
        except Exception:
            try:
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(path), self.AVATAR_SIZE, self.AVATAR_SIZE, True
                )
            except Exception:
                return None

    def _draw_avatar(self, widget: Gtk.DrawingArea, cr: Any) -> bool:
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        if self._base_pixbuf is None:
            self._base_pixbuf = self._load_base_pixbuf()

        cx, cy = w / 2.0, h / 2.0
        cr.save()
        if hasattr(cr, "set_antialias"):
            cr.set_antialias(_cairo.ANTIALIAS_BEST)

        # Apply living tilt/scale with centered origin
        cr.translate(cx, cy)
        if self._avatar_angle:
            cr.rotate(self._avatar_angle)
        if self._avatar_scale != 1.0:
            cr.scale(self._avatar_scale, self._avatar_scale)

        if self._base_pixbuf:
            pw = self._base_pixbuf.get_width()
            ph = self._base_pixbuf.get_height()
            # No gray box: paint with alpha preservation, crisp
            Gdk.cairo_set_source_pixbuf(cr, self._base_pixbuf, -pw / 2.0, -ph / 2.0)
            cr.paint()
        else:
            # Theme-palette fallback: no gray boxes, use theme surface + accent
            try:
                r, g, b = hex_to_rgb(self._theme.surface)
                cr.set_source_rgba(r, g, b, 0.9)
                cr.arc(0, 0, min(w, h) * 0.42, 0, 2 * math.pi)
                cr.fill()
                r2, g2, b2 = hex_to_rgb(self._theme.accent)
                cr.set_source_rgba(r2, g2, b2, 1.0)
                cr.select_font_face(
                    "monospace", _cairo.FONT_SLANT_NORMAL, _cairo.FONT_WEIGHT_BOLD
                )
                cr.set_font_size(44)
                text = self._agent[0].upper() if self._agent else "?"
                ext = cr.text_extents(text)
                cr.move_to(-ext.width / 2.0, ext.height / 2.0)
                cr.show_text(text)
            except Exception:
                pass
        cr.restore()
        return False

    def _refresh_avatar(self) -> None:
        self._base_pixbuf = None
        old = self._avatar
        self._avatar = self._make_avatar_area()
        parent = old.get_parent()
        if parent:
            parent.remove(old)
            parent.pack_start(self._avatar, True, True, 0)
            parent.reorder_child(self._avatar, 0)
            parent.show_all()

    def _make_clickthrough(self, *args):
        if self._clickthrough:
            self._win.set_accept_focus(False)
            gdkwin = self._win.get_window()
            if gdkwin:
                gdkwin.set_pass_through(True)
        else:
            self._win.set_accept_focus(True)
            gdkwin = self._win.get_window()
            if gdkwin:
                gdkwin.set_pass_through(False)

    def _sigusr2(self, _signum, _frame):
        states = list(ThootieState)
        idx = states.index(self.machine.state)
        self.machine.set_state(states[(idx + 1) % len(states)])

    def _advance_corner(self) -> None:
        if PATROL_ENABLED:
            self._waypoint_index = self._target_index
            self._target_index = (self._target_index + 1) % len(_WAYPOINTS)

    def _on_state_change(self, *args) -> None:
        changed = False
        new_state = None
        if len(args) == 2 and isinstance(args[0], ThootieState) and args[0] != args[1]:
            changed = True
            new_state = args[1]
            self._advance_corner()
            if new_state == ThootieState.DENIED:
                self._shake_remaining = self.SHAKE_DURATION
            elif new_state == ThootieState.APPROVED_RUN:
                self._flourish_remaining = self.FLOURISH_DURATION
            elif new_state == ThootieState.APPROVAL_INCOMING:
                # Spawn flourish for incoming approval — pulse incoming
                self._spawn_remaining = self.SPAWN_DURATION
                self._spawn_progress = 0.0
            self._state_pulse = 0.0

        self._state_label.set_text(self.machine.state.value.upper())
        self._win.queue_draw()

    def set_agent(self, agent: str) -> None:
        self._agent = agent
        self._agent_label.set_text(agent.upper())
        self._title_label.set_text(f"{agent.upper()} RUN CARD")
        self._refresh_avatar()

    def set_card_text(self, text: str) -> None:
        self._card_label.set_text(text)

    def _set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self._agent_label.show()
            self._state_label.show()
            self._right.set_no_show_all(False)
            self._right.show_all()
            self._clickthrough = False
            self._css_provider.load_from_data(_build_css_active(self._theme))
            # Spawn animation: card emerges FROM avatar
            self._spawn_remaining = self.SPAWN_DURATION
            self._spawn_progress = 0.0
            # Start small, grow to active size for spawn-from-avatar feel
            self._win.resize(self.IDLE_SIZE + 20, self.IDLE_SIZE + 20)
            self._clamp_on_screen(self.ACTIVE_WIDTH, self.ACTIVE_HEIGHT)
        else:
            self._agent_label.hide()
            self._state_label.hide()
            self._right.set_no_show_all(True)
            self._right.hide()
            self._win.resize(self.IDLE_SIZE, self.IDLE_SIZE)
            self._clickthrough = True
            self._css_provider.load_from_data(_build_css(self._theme))
            self._spawn_progress = 1.0
            self._spawn_remaining = 0
        self._make_clickthrough()

    def clear_buttons(self) -> None:
        for child in self._button_box.get_children():
            self._button_box.remove(child)
        self._button_box.set_no_show_all(True)
        self._button_box.hide()

    def add_approval_buttons(
        self,
        labels: dict[str, str] | None = None,
        callback: Callable[[str], None] | None = None,
    ) -> None:
        self.clear_buttons()
        labels = labels or {
            "once": "Approve once",
            "session": "Approve session",
            "always": "Approve always",
            "deny": "Deny",
        }
        for key, label in labels.items():
            btn = Gtk.Button(label=label)
            btn.set_can_focus(False)
            btn.connect("clicked", lambda _b, k=key: callback(k) if callback else None)
            self._button_box.pack_start(btn, True, True, 0)
        self._button_box.set_no_show_all(False)
        self._button_box.show_all()

    def _finish_confirm(self, verdict: str) -> None:
        self._approval_verdict = verdict
        if self._approval_loop and self._approval_loop.is_running():
            self._approval_loop.quit()

    def _show_always_confirm(self) -> None:
        self.clear_buttons()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.END)
        label = Gtk.Label(label="Approve always?")
        label.set_name("card")
        box.pack_start(label, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        yes = Gtk.Button(label="Yes")
        yes.set_can_focus(False)
        yes.get_style_context().add_class("success")
        yes.connect("clicked", lambda _b: self._finish_confirm("always"))
        no = Gtk.Button(label="No")
        no.set_can_focus(False)
        no.connect("clicked", lambda _b: self._finish_confirm("deny"))
        btn_box.pack_start(yes, True, True, 0)
        btn_box.pack_start(no, True, True, 0)
        box.pack_start(btn_box, False, False, 0)

        self._button_box.pack_start(box, True, True, 0)
        self._button_box.set_no_show_all(False)
        self._button_box.show_all()

    def present_approval(self, timeout: int = 120) -> str:
        if self._approval_loop is not None:
            return "deny"
        self._approval_verdict = None
        self._approval_loop = GLib.MainLoop()

        def on_verdict(key: str) -> None:
            if key == "always":
                self._show_always_confirm()
            else:
                self._finish_confirm(key)

        self.add_approval_buttons(callback=on_verdict)
        self._set_active(True)
        self.machine.set_state(
            ThootieState.APPROVAL_PRESENTED,
            StatePayload(action_kind="approval"),
        )

        self._timer_id: Any = None
        if timeout > 0:
            self._timer_id = GLib.timeout_add_seconds(timeout, self._on_timeout)

        self._approval_loop.run()

        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        self._approval_loop = None

        verdict = self._approval_verdict or "deny"
        self.clear_buttons()
        return verdict

    def _on_timeout(self) -> bool:
        if self._approval_loop and self._approval_loop.is_running():
            self._approval_verdict = "deny"
            self._approval_loop.quit()
        return False

    def park_idle(self) -> None:
        self.clear_buttons()
        self.set_card_text("")
        self._set_active(False)
        self.machine.set_state(ThootieState.IDLE)

    def show(self) -> None:
        x, y = self._waypoint_px(self._waypoint_index)
        self._pos = [float(x), float(y)]
        self._win.show_all()
        self._win.move(x, y)
        if self._active:
            self._clamp_on_screen(self.ACTIVE_WIDTH, self.ACTIVE_HEIGHT)
        GLib.timeout_add(self.TICK_MS, self._drift_tick)

    def _waypoint_px(self, index: int) -> tuple[int, int]:
        screen = self._win.get_screen()
        sw = screen.get_width()
        sh = screen.get_height()
        fx, fy = _WAYPOINTS[index % len(_WAYPOINTS)]
        # Clamp active window fully on-screen; idle is small so original math works
        base_w = self.ACTIVE_WIDTH if self._active else self.IDLE_SIZE
        base_h = self.ACTIVE_HEIGHT if self._active else self.IDLE_SIZE
        return (int((sw - base_w) * fx), int((sh - base_h) * fy))

    def _clamp_on_screen(self, w: int, h: int) -> None:
        screen = self._win.get_screen()
        sw = screen.get_width()
        sh = screen.get_height()
        x, y = self._win.get_position()
        self._win.move(max(0, min(x, sw - w)), max(0, min(y, sh - h)))

    def _on_drag_begin(self, _w, event) -> bool:
        """Draggable card (2026-10-31): like the slide keyboard. Button
        presses on approval buttons are consumed first, so clicks work."""
        try:
            x, y = self._win.get_position()
            self._drag = (event.x_root - x, event.y_root - y)
        except Exception:
            self._drag = None
        return False

    def _on_drag_motion(self, _w, event) -> bool:
        if not self._drag:
            return False
        try:
            dx, dy = self._drag
            self._pos = [event.x_root - dx, event.y_root - dy]
            self._win.move(int(self._pos[0]), int(self._pos[1]))
        except Exception:
            pass
        return False

    def _on_drag_end(self, _w, _event) -> bool:
        self._drag = None
        w, h = self._win.get_size()
        self._clamp_on_screen(w, h)
        return False

    def _drift_tick(self) -> bool:
        """Single motion authority: one tick composes bob+drift, capped delta."""
        self._state_pulse += 0.12
        self._shake_phase += 0.2

        bob_x, bob_y, base_tilt, base_scale = self._living_idle.tick()

        st = self.machine.state
        tilt_mod = 0.0
        scale_mod = 0.0

        if st == ThootieState.IDLE:
            scale_mod = 0.006 * math.sin(self._state_pulse * 0.9)
            tilt_mod = 0.01 * math.sin(self._state_pulse * 0.4)
        elif st == ThootieState.WORKING:
            scale_mod = 0.01 * math.sin(self._state_pulse * 1.2)
            tilt_mod = 0.02 * math.sin(self._state_pulse * 0.5)
            bob_y *= 1.05
        elif st == ThootieState.SORTING:
            tilt_mod = 0.03 * math.sin(self._state_pulse * 0.5)
            scale_mod = 0.008 * math.sin(self._state_pulse * 0.4)
        elif st == ThootieState.APPROVAL_INCOMING:
            scale_mod = 0.02 * abs(math.sin(self._state_pulse * 1.5))
            tilt_mod = 0.015 * math.sin(self._state_pulse * 1.0)
        elif st == ThootieState.APPROVAL_PRESENTED:
            scale_mod = 0.01 * math.sin(self._state_pulse * 0.8)
            tilt_mod = 0.01 * math.sin(self._state_pulse * 0.5)
        elif st == ThootieState.EDIT:
            tilt_mod = 0.02 * math.sin(self._state_pulse * 1.2)
            scale_mod = 0.01 * math.sin(self._state_pulse * 1.5)

        if self._spawn_remaining > 0:
            self._spawn_remaining -= 1
            self._spawn_progress = 1.0 - (self._spawn_remaining / self.SPAWN_DURATION)
            t = self._spawn_progress
            smooth = t * t * (3 - 2 * t)
            cur_w = int(self.IDLE_SIZE + (self.ACTIVE_WIDTH - self.IDLE_SIZE) * smooth)
            cur_h = int(self.IDLE_SIZE + (self.ACTIVE_HEIGHT - self.IDLE_SIZE) * smooth)
            if self._active:
                self._win.resize(cur_w, cur_h)
                self._clamp_on_screen(cur_w, cur_h)
            scale_mod += 0.03 * math.sin(smooth * math.pi)
            if self._spawn_remaining == 0:
                self._spawn_progress = 1.0
                if self._active:
                    self._win.resize(self.ACTIVE_WIDTH, self.ACTIVE_HEIGHT)
        else:
            self._spawn_progress = 1.0

        # Subtle flourishes: no spin, tiny scale/tilt only
        sx = sy = 0.0
        if self._shake_remaining > 0:
            self._shake_remaining -= 1
            sx = math.sin(self._shake_phase * 8.0) * self.SHAKE_AMP * 0.5
            sy = math.cos(self._shake_phase * 6.0) * self.SHAKE_AMP * 0.5
            tilt_mod += 0.02 * math.sin(self._shake_phase * 1.5)
            if self._shake_remaining == 0:
                sx = sy = 0.0

        if self._flourish_remaining > 0:
            self._flourish_remaining -= 1
            t = 1.0 - self._flourish_remaining / self.FLOURISH_DURATION
            # subtle: small scale bump, zero spin
            self._avatar_scale = (
                1.0 + 0.08 * math.sin(t * math.pi) + scale_mod + (base_scale - 1.0)
            )
            self._avatar_angle = base_tilt + tilt_mod
            if self._flourish_remaining == 0:
                self._avatar_scale = base_scale + scale_mod
                self._avatar_angle = base_tilt + tilt_mod
        else:
            self._avatar_scale = base_scale + scale_mod
            self._avatar_angle = base_tilt + tilt_mod

        # --- ONE motion authority: compose final desired position, then cap delta ---
        base_x, base_y = self._waypoint_px(self._waypoint_index)
        if self.machine.state in (
            ThootieState.APPROVAL_INCOMING,
            ThootieState.APPROVAL_PRESENTED,
            ThootieState.EDIT,
        ):
            des_x, des_y = self._pos[0], self._pos[1]
        elif PATROL_ENABLED and not self._active:
            tx, ty = self._waypoint_px(self._target_index)
            nx, ny = drift_step((self._pos[0], self._pos[1]), (tx, ty), 0.004)
            self._pos = [nx, ny]
            des_x = nx + bob_x + sx
            des_y = ny + bob_y + sy
        else:
            des_x = base_x + bob_x + sx
            des_y = base_y + bob_y + sy
            self._pos = [base_x, base_y]

        # Cap total displacement per tick to few px
        if not hasattr(self, "_last_win_x"):
            self._last_win_x, self._last_win_y = des_x, des_y
        dx = des_x - self._last_win_x
        dy = des_y - self._last_win_y
        cap = self.MAX_DISP_PER_TICK
        if abs(dx) > cap:
            des_x = self._last_win_x + math.copysign(cap, dx)
        if abs(dy) > cap:
            des_y = self._last_win_y + math.copysign(cap, dy)
        self._last_win_x, self._last_win_y = des_x, des_y

        self._win.move(int(des_x), int(des_y))
        self._avatar.queue_draw()
        return True

    def current_corner(self) -> str:
        names = ["top-left", "bottom-left", "bottom-right"]
        return names[self._waypoint_index % len(names)]

    def current_position(self) -> tuple[int, int]:
        return (int(self._pos[0]), int(self._pos[1]))

    def run(self) -> None:
        self.show()
        Gtk.main()


_singleton_overlay: ThootieOverlay | None = None


def get_overlay(agent: str = "ninja", waypoint_index: int = 0) -> ThootieOverlay | None:
    global _singleton_overlay
    if not _GTK_AVAILABLE:
        return None
    if _singleton_overlay is None:
        try:
            _singleton_overlay = ThootieOverlay(
                waypoint_index=waypoint_index, agent=agent
            )
        except RuntimeError:
            return None
    return _singleton_overlay


def show_avatar(agent: str, card: dict | str, waypoint_index: int = 0) -> None:
    overlay = get_overlay(agent=agent, waypoint_index=waypoint_index)
    if overlay is None:
        return
    overlay.set_agent(agent)
    if isinstance(card, dict):
        from ninja.card_box import render_card_text

        text = render_card_text(card)
    else:
        text = str(card)
    overlay.set_card_text(text)
    overlay._set_active(True)
    overlay.machine.set_state(ThootieState.WORKING, StatePayload(action_kind="run"))
    if not overlay._win.get_visible():
        overlay.show()


def park_idle() -> None:
    global _singleton_overlay
    if _singleton_overlay is not None:
        _singleton_overlay.park_idle()


def _demo() -> None:
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

    from gi.repository import GLib

    GLib.timeout_add(2000, cycle)
    overlay.run()


if __name__ == "__main__":
    _demo()
