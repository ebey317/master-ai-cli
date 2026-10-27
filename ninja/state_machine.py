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
import os
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
# GTK overlay: floating persistent avatar overlay.
# ---------------------------------------------------------------------------

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

    _GTK_AVAILABLE = True
except Exception:  # pragma: no cover - GTK may be absent during py_compile
    _GTK_AVAILABLE = False
    Gtk = None  # type: ignore
    Gdk = None  # type: ignore
    GdkPixbuf = None  # type: ignore


# Waypoint path as fractions of the screen: top-left -> bottom-left ->
# bottom-right. Top-right is the notification corner: exclusion zone,
# the avatar never parks there.
_WAYPOINTS = [
    (0.02, 0.02),  # top-left
    (0.02, 0.98),  # bottom-left
    (0.98, 0.98),  # bottom-right
]
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
    background-color: {theme.background};
}}
#panel {{
    background-color: {theme.surface};
    border: 2px solid {theme.accent};
    border-radius: 16px;
    padding: 12px;
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


class ThootieOverlay:
    """Floating persistent avatar overlay.

    Renders the requested agent's avatar PNG at the current corner waypoint.
    The overlay stays up for an entire run, expands to show a live card, and
    approval prompts reuse the same visible window.  Idle mode is small
    (~220px) and click-through; active/approval mode is larger so the card and
    buttons are readable.
    """

    IDLE_SIZE = 220
    AVATAR_SIZE = 196
    ACTIVE_WIDTH = 640
    ACTIVE_HEIGHT = 360

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

        self._win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self._win.set_keep_above(True)
        self._win.set_decorated(False)
        self._win.set_skip_taskbar_hint(True)
        self._win.set_skip_pager_hint(True)
        self._win.set_accept_focus(False)
        self._win.set_default_size(self.IDLE_SIZE, self.IDLE_SIZE)

        screen = self._win.get_screen()
        provider = Gtk.CssProvider()
        provider.load_from_data(_build_css(self._theme))
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._panel = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._panel.set_name("panel")
        self._win.add(self._panel)

        # Left: avatar + agent/state labels.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left.set_size_request(self.IDLE_SIZE, self.IDLE_SIZE)
        self._avatar = self._make_avatar_image()
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

        self.machine = ThootieStateMachine(
            redraw_hook=self._on_state_change,
            state_change_hook=self._on_state_change,
        )
        self._win.connect("realize", self._make_clickthrough)
        self._win.connect("map-event", self._make_clickthrough)

        signal.signal(signal.SIGUSR2, self._sigusr2)

    def _make_avatar_image(self) -> Any:
        path = _avatar_path(self._agent)
        if path:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(path), self.AVATAR_SIZE, self.AVATAR_SIZE, True
                )
                return Gtk.Image.new_from_pixbuf(pixbuf)
            except Exception:
                pass
        fallback = Gtk.Label(label=self._agent[0].upper() if self._agent else "?")
        fallback.set_name("title")
        fallback.set_size_request(self.AVATAR_SIZE, self.AVATAR_SIZE)
        return fallback

    def _refresh_avatar(self) -> None:
        old = self._avatar
        self._avatar = self._make_avatar_image()
        # Replace in parent left box (first child).
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

    def _on_state_change(self, *args) -> None:
        self._state_label.set_text(self.machine.state.value.upper())
        field = self._PALETTE.get(self.machine.state, "accent")
        # Refresh border color via CSS reload is expensive; keep it simple.
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
            self._right.set_no_show_all(False)
            self._right.show_all()
            self._win.resize(self.ACTIVE_WIDTH, self.ACTIVE_HEIGHT)
            self._clickthrough = False
        else:
            self._right.set_no_show_all(True)
            self._right.hide()
            self._win.resize(self.IDLE_SIZE, self.IDLE_SIZE)
            self._clickthrough = True
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
        GLib.timeout_add(50, self._drift_tick)

    def _waypoint_px(self, index: int) -> tuple[int, int]:
        screen = self._win.get_screen()
        sw = screen.get_width()
        sh = screen.get_height()
        fx, fy = _WAYPOINTS[index % len(_WAYPOINTS)]
        return (int((sw - self.IDLE_SIZE) * fx), int((sh - self.IDLE_SIZE) * fy))

    def _drift_tick(self) -> bool:
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
