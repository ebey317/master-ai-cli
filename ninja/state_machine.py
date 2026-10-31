#!/usr/bin/env python3
"""Thootie — Master AI desktop companion (avatar window + states).

Stage 3: persistent avatar presence for a whole headless run.  The overlay
shows the agent avatar, expands to carry a live WHO/WHAT/WHERE/WHEN/HOW/DELEGATED
card, and approval prompts reuse the same visible window.  When the run ends the
avatar shrinks back to an idle corner pose.

Display contract (operator spec 2026-10-26/27): small always (~220px) when idle,
never modal, never full-screen, notification corner stays clear.
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

# Make scripts.theme importable when this module is loaded stand-alone.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

from scripts.theme import Theme, load_theme


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
    alpha: float = 0.04,
) -> tuple[float, float]:
    """Ease ``current`` toward ``target``; ``alpha`` in (0, 1]."""
    if not 0 < alpha <= 1:
        raise ValueError("alpha must be in (0, 1]")
    return (
        current[0] + (target[0] - current[0]) * alpha,
        current[1] + (target[1] - current[1]) * alpha,
    )


class LivingIdle:
    """Organic, non-periodic idle motion generator.

    Instead of a fixed sine bob, amplitude, effective period and phase are all
    random-walked and reseeded at irregular intervals.  Random pauses, slow
    tilt/look shifts, and occasional scale changes keep the motion alive and
    never visibly periodic.
    """

    def __init__(self, tick_s: float = 0.05) -> None:
        self.tick_s = tick_s
        self.bob_offset = 0.0
        self.bob_amp = 3.0
        self.bob_period_s = 4.0
        self.bob_phase = random.random() * 2.0 * math.pi
        self.bob_target = 0.0
        self.tilt = 0.0
        self.tilt_target = 0.0
        self.scale = 1.0
        self.scale_target = 1.0
        self.reseed_ticks = 0
        self.pause_ticks = 0
        self.corner_timer = 0
        self._reseed()

    def _reseed(self) -> None:
        """Pick fresh motion parameters; called at irregular intervals."""
        self.bob_amp = random.uniform(1.5, 7.0)
        self.bob_period_s = random.uniform(2.5, 7.0)
        # Random phase jump on reseed so the waveform never realigns.
        self.bob_phase = random.random() * 2.0 * math.pi
        self.reseed_ticks = random.randint(40, 120)  # 2-6 s at 50 ms ticks
        self.bob_target = random.uniform(-self.bob_amp, self.bob_amp)
        self.tilt_target = random.uniform(-0.12, 0.12)
        self.scale_target = random.uniform(0.96, 1.06)

    def tick(self) -> tuple[float, float, float]:
        """Return ``(bob_offset, tilt_angle, scale)`` for this frame."""
        if self.reseed_ticks <= 0:
            self._reseed()
        else:
            self.reseed_ticks -= 1

        if self.pause_ticks > 0:
            self.pause_ticks -= 1
            return (self.bob_offset, self.tilt, self.scale)

        # Occasional random pause: 0.5-2 s, roughly once every ~30 s.
        if random.random() < 0.003:
            self.pause_ticks = random.randint(10, 40)

        # Random-walk the bob target while the sine component advances with a
        # drifting period/phase; smooth the result so it feels like breath.
        omega = 2.0 * math.pi / self.bob_period_s
        self.bob_phase += omega * self.tick_s
        sine_target = math.sin(self.bob_phase) * self.bob_amp
        # Add a second, slower non-harmonic term so no clean loop forms.
        sine_target += (
            0.35 * math.sin(self.bob_phase * 0.31 + random.random()) * self.bob_amp
        )
        # Random-walk perturbation on top of the sine.
        if random.random() < 0.08:
            self.bob_target = random.uniform(-self.bob_amp, self.bob_amp)
        target = 0.6 * sine_target + 0.4 * self.bob_target
        self.bob_offset = drift_step((self.bob_offset, 0.0), (target, 0.0), 0.08)[0]

        # Slow tilt/look moves.
        if random.random() < 0.02:
            self.tilt_target = random.uniform(-0.12, 0.12)
        self.tilt = drift_step((self.tilt, 0.0), (self.tilt_target, 0.0), 0.05)[0]

        # Subtle scale breathing.
        if random.random() < 0.015:
            self.scale_target = random.uniform(0.96, 1.05)
        self.scale = drift_step((self.scale, 0.0), (self.scale_target, 0.0), 0.04)[0]

        return (self.bob_offset, self.tilt, self.scale)

    def maybe_corner(self, current: int, num_corners: int) -> int | None:
        """Return a new corner index to drift toward, or ``None``."""
        if self.corner_timer <= 0:
            self.corner_timer = random.randint(100, 400)  # 5-20 s
            choices = [i for i in range(num_corners) if i != current]
            if choices:
                return random.choice(choices)
        self.corner_timer -= 1
        return None


# ---------------------------------------------------------------------------
# GTK overlay: floating persistent avatar overlay.
# ---------------------------------------------------------------------------

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    import cairo as _cairo
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

    _GTK_AVAILABLE = True
except Exception:  # pragma: no cover - GTK may be absent during py_compile
    _GTK_AVAILABLE = False
    Gtk = None  # type: ignore
    Gdk = None  # type: ignore
    GdkPixbuf = None  # type: ignore
    _cairo = None  # type: ignore


# Owner's patrol route, in order: top-left -> bottom-left -> bottom-right ->
# bottom-left -> top-left. Straight legs, slow, hold the pattern.
# Top-right is the notification corner: exclusion zone, never parked.
_WAYPOINTS = [
    (0.02, 0.02),  # top-left
    (0.02, 0.98),  # bottom-left
    (0.98, 0.98),  # bottom-right
    (0.02, 0.98),  # back to bottom-left
    (0.02, 0.02),  # back to top-left
]
# Slow patrol: fraction of remaining distance closed per 50ms tick.
PATROL_ALPHA = 0.008
# Owner's exact route: no random corner jumps while True.
PATROL_EXACT_ROUTE = True
_EXCLUDED_NOTIFICATION_CORNER = (0.98, 0.02)  # top-right

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
    """Best-effort DISPLAY setup for headless imports."""
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
#title {{
    color: {theme.accent};
    font-family: monospace;
    font-size: 14px;
    font-weight: bold;
    margin-bottom: 6px;
}}
#state {{
    color: {theme.text_secondary};
    font-family: monospace;
    font-size: 11px;
    margin-top: 4px;
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


# Opaque approval skin (2026-10-31): the idle skin is transparent by design,
# but an approval card must be solid — see-through text over the operator's
# work is unreadable. Swapped in by _set_active(True); later rules win, so
# these override the transparent window/panel above.
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
    """Floating persistent avatar overlay.

    Renders the requested agent's avatar PNG at the current corner waypoint.
    The overlay stays up for an entire run, expands to show a live card, and
    approval prompts reuse the same visible window.  Idle mode is small
    (~220px) and click-through; active/approval mode is larger so the card and
    buttons are readable.

    Motion is driven by a single GLib timer:
        - living idle motion when IDLE (random-walk bob/tilt/scale, random
          pauses, occasional corner drifts; never a fixed loop)
        - drift to the next parked corner on every state change
        - window shake on DENIED
        - scale + spin flourish on APPROVED_RUN
    """

    IDLE_SIZE = 128
    AVATAR_SIZE = 112
    ACTIVE_WIDTH = 380
    ACTIVE_HEIGHT = 260

    # animation constants
    SHAKE_AMP = 8
    SHAKE_DURATION = 30  # 50 ms ticks -> ~1.5 s
    FLOURISH_DURATION = 50  # ~2.5 s
    FLOURISH_SPIN = 2.0

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
        # Use the screen's RGBA visual so un-painted regions are fully transparent.
        visual = screen.get_rgba_visual()
        if visual is not None:
            self._win.set_visual(visual)
        self._win.set_app_paintable(True)
        self._css_provider = Gtk.CssProvider()
        self._css_provider.load_from_data(_build_css(self._theme))
        Gtk.StyleContext.add_provider_for_screen(
            screen, self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._panel = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._panel.set_name("panel")
        self._win.add(self._panel)

        # Left: avatar + agent/state labels.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left.set_size_request(self.IDLE_SIZE, self.IDLE_SIZE)
        self._avatar = self._make_avatar_area()
        left.pack_start(self._avatar, True, True, 0)

        self._agent_label = Gtk.Label(label=self._agent.upper())
        self._agent_label.set_name("title")
        left.pack_start(self._agent_label, False, False, 0)

        self._state_label = Gtk.Label(label=ThootieState.IDLE.value.upper())
        self._state_label.set_name("state")
        left.pack_start(self._state_label, False, False, 0)

        self._panel.pack_start(left, False, False, 0)

        # Right: active content (card + optional buttons). Hidden in idle mode.
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
        self._target_index = (self._waypoint_index + 1) % len(_WAYPOINTS)
        self._pos: list[float] = [0.0, 0.0]
        self._pulse_on = False
        self._clickthrough = True
        self._active = False
        self._approval_verdict: str | None = None
        self._approval_loop: Any | None = None

        # animation state
        self._living_idle = LivingIdle(tick_s=0.05)
        self._shake_phase = 0.0
        self._avatar_scale = 1.0
        self._avatar_angle = 0.0
        self._shake_remaining = 0
        self._flourish_remaining = 0

        self.machine = ThootieStateMachine(
            redraw_hook=self._on_state_change,
            state_change_hook=self._on_state_change,
        )
        self._win.connect("realize", self._make_clickthrough)
        self._win.connect("map-event", self._make_clickthrough)

        signal.signal(signal.SIGUSR2, self._sigusr2)

        self._set_active(False)

    def _make_avatar_area(self) -> Any:
        """Return a DrawingArea whose draw callback applies live transforms."""
        area = Gtk.DrawingArea()
        area.set_size_request(self.AVATAR_SIZE, self.AVATAR_SIZE)
        area.connect("draw", self._draw_avatar)
        return area

    def _draw_avatar(self, widget: Gtk.DrawingArea, cr: Any) -> bool:
        """Paint the avatar with current scale/angle."""
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        path = _avatar_path(self._agent)
        if self._base_pixbuf is None and path:
            try:
                self._base_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(path), self.AVATAR_SIZE, self.AVATAR_SIZE, True
                )
            except Exception:
                self._base_pixbuf = None

        cx, cy = w / 2.0, h / 2.0
        cr.save()
        cr.translate(cx, cy)
        if self._avatar_angle:
            cr.rotate(self._avatar_angle)
        if self._avatar_scale != 1.0:
            cr.scale(self._avatar_scale, self._avatar_scale)

        if self._base_pixbuf:
            pw = self._base_pixbuf.get_width()
            ph = self._base_pixbuf.get_height()
            Gdk.cairo_set_source_pixbuf(cr, self._base_pixbuf, -pw / 2.0, -ph / 2.0)
            cr.paint()
        else:
            cr.set_source_rgb(0.9, 0.9, 0.9)
            cr.select_font_face(
                "monospace", _cairo.FONT_SLANT_NORMAL, _cairo.FONT_WEIGHT_BOLD
            )
            cr.set_font_size(48)
            text = self._agent[0].upper() if self._agent else "?"
            ext = cr.text_extents(text)
            cr.move_to(-ext.width / 2.0, ext.height / 2.0)
            cr.show_text(text)
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
        """Pick the next parked corner as the drift target."""
        self._waypoint_index = self._target_index
        self._target_index = (self._target_index + 1) % len(_WAYPOINTS)

    def _on_state_change(self, *args) -> None:
        # The state-change hook passes (old_state, new_state); the redraw hook
        # passes no arguments.
        if len(args) == 2 and isinstance(args[0], ThootieState) and args[0] != args[1]:
            self._advance_corner()
            new_state = args[1]
            if new_state == ThootieState.DENIED:
                self._shake_remaining = self.SHAKE_DURATION
            elif new_state == ThootieState.APPROVED_RUN:
                self._flourish_remaining = self.FLOURISH_DURATION
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
            self._win.resize(self.ACTIVE_WIDTH, self.ACTIVE_HEIGHT)
            self._clickthrough = False
            # Opaque approval skin (see _build_css_active): the idle skin is
            # transparent by design, but cards must be solid to be readable.
            self._css_provider.load_from_data(_build_css_active(self._theme))
            # The waypoint anchor assumes IDLE_SIZE: a 480px window parked at
            # the 0.98 waypoint hangs off the screen (observed 2026-10-31).
            # Clamp after every resize so the card stays fully visible.
            self._clamp_on_screen(self.ACTIVE_WIDTH, self.ACTIVE_HEIGHT)
        else:
            self._agent_label.hide()
            self._state_label.hide()
            self._right.set_no_show_all(True)
            self._right.hide()
            self._win.resize(self.IDLE_SIZE, self.IDLE_SIZE)
            self._clickthrough = True
            self._css_provider.load_from_data(_build_css(self._theme))
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
        """Replace approval buttons with an inline Yes/No always confirmation."""
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
        """Show approval buttons and block until a verdict is chosen."""
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
        """Park the overlay back to the idle corner pose and clear the card."""
        self.clear_buttons()
        self.set_card_text("")
        self._set_active(False)
        self.machine.set_state(ThootieState.IDLE)

    def show(self) -> None:
        x, y = self._waypoint_px(self._waypoint_index)
        self._pos = [float(x), float(y)]
        self._win.show_all()
        self._win.move(x, y)
        # show() runs after _set_active: re-clamp, because the waypoint
        # anchor assumes IDLE_SIZE and the active card would hang off-screen
        # (observed 2026-10-31: y=932 on a 1080p screen).
        if self._active:
            self._clamp_on_screen(self.ACTIVE_WIDTH, self.ACTIVE_HEIGHT)
        GLib.timeout_add(50, self._drift_tick)

    def _waypoint_px(self, index: int) -> tuple[int, int]:
        screen = self._win.get_screen()
        sw = screen.get_width()
        sh = screen.get_height()
        fx, fy = _WAYPOINTS[index % len(_WAYPOINTS)]
        return (int((sw - self.IDLE_SIZE) * fx), int((sh - self.IDLE_SIZE) * fy))

    def _clamp_on_screen(self, w: int, h: int) -> None:
        """Move the window so a w×h box at its position stays on-screen."""
        screen = self._win.get_screen()
        sw = screen.get_width()
        sh = screen.get_height()
        x, y = self._win.get_position()
        self._win.move(max(0, min(x, sw - w)), max(0, min(y, sh - h)))

    def _drift_tick(self) -> bool:
        # Frozen outside IDLE (2026-10-31): approvals must not float around —
        # the card stays parked where it appeared until the verdict lands.
        if self.machine.state != ThootieState.IDLE:
            return True
        tx, ty = self._waypoint_px(self._target_index)
        nx, ny = drift_step((self._pos[0], self._pos[1]), (tx, ty), PATROL_ALPHA)
        self._pos = [nx, ny]

        # Living idle motion: organic, never periodic. Only active in IDLE.
        bob_y = 0.0
        if self.machine.state == ThootieState.IDLE:
            bob_y, self._avatar_angle, self._avatar_scale = self._living_idle.tick()
            if not PATROL_EXACT_ROUTE:
                # Occasional random corner drift while idle.
                new_corner = self._living_idle.maybe_corner(
                    self._target_index, len(_WAYPOINTS)
                )
                if new_corner is not None and new_corner != self._target_index:
                    self._target_index = new_corner
        # Patrol: arrived within a few px -> next leg of the route.
        if abs(nx - tx) < 8 and abs(ny - ty) < 8:
            self._waypoint_index = self._target_index
            self._target_index = (self._target_index + 1) % len(_WAYPOINTS)
        else:
            # Advance a dedicated shake phase even when not bobbing.
            self._shake_phase += 0.5
            self._living_idle.tick()  # keep generator warm, but ignore output

        # Denied shake: jitter the window around the eased position.
        sx = sy = 0.0
        if self._shake_remaining > 0:
            self._shake_remaining -= 1
            sx = math.sin(self._shake_phase * 23.0) * self.SHAKE_AMP
            sy = math.cos(self._shake_phase * 19.0) * self.SHAKE_AMP
            if self._shake_remaining == 0:
                sx = sy = 0.0

        # Done flourish: scale + spin the avatar.
        if self._flourish_remaining > 0:
            self._flourish_remaining -= 1
            t = 1.0 - self._flourish_remaining / self.FLOURISH_DURATION
            self._avatar_scale = 1.0 + 0.35 * math.sin(t * math.pi)
            self._avatar_angle = (
                self.FLOURISH_SPIN * 2 * math.pi * (1 - math.cos(t * math.pi)) / 2
            )
            if self._flourish_remaining == 0:
                self._avatar_scale = 1.0
                self._avatar_angle = 0.0
        elif self.machine.state != ThootieState.IDLE:
            # Return to neutral.
            self._avatar_scale = drift_step((self._avatar_scale, 0.0), (1.0, 0.0))[0]
            self._avatar_angle = drift_step(
                (self._avatar_angle, 0.0), (0.0, 0.0), 0.08
            )[0]

        self._win.move(int(nx + sx), int(ny + bob_y + sy))
        self._avatar.queue_draw()
        return True

    def current_corner(self) -> str:
        """Return the current corner name."""
        names = ["top-left", "bottom-left", "bottom-right"]
        return names[self._waypoint_index % len(names)]

    def current_position(self) -> tuple[int, int]:
        return (int(self._pos[0]), int(self._pos[1]))

    def run(self) -> None:
        self.show()
        Gtk.main()


_singleton_overlay: ThootieOverlay | None = None


def get_overlay(agent: str = "ninja", waypoint_index: int = 0) -> ThootieOverlay | None:
    """Return the singleton overlay, creating it if needed."""
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
    """Bring up the persistent avatar overlay with a live card.

    Refreshes the card text in place if the overlay is already visible.  Sets
    the state machine to WORKING.  The caller is responsible for calling
    ``park_idle()`` when the run ends.
    """
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
    """Park the persistent avatar back to idle."""
    global _singleton_overlay
    if _singleton_overlay is not None:
        _singleton_overlay.park_idle()


def _demo() -> None:
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
