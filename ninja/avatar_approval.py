#!/usr/bin/env python3
"""Avatar-driven approval surface for Tahuti.

Reuses the Ninja overlay scaffold (corners, theme palette) and the card_box
renderer.  Displays the per-agent avatar PNG beside the approval card with
four decision buttons.  Click/choose = verdict; timeout = deny; fail-closed.

Usage from Tahuti:
    from ninja.avatar_approval import ask_avatar_approval
    verdict = ask_avatar_approval(card, agent="tahuti")

Returns one of: "once" | "session" | "always" | "deny".
"""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=DeprecationWarning)

_GTK_AVAILABLE = False
Gtk = None  # type: ignore
GLib = None  # type: ignore
Gdk = None  # type: ignore
GdkPixbuf = None  # type: ignore
_GTK_ERROR: str | None = None

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # type: ignore

    _GTK_AVAILABLE = True
except Exception as exc:
    _GTK_ERROR = str(exc)
    _GTK_AVAILABLE = False

# Make scripts.theme importable when this module is loaded stand-alone.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

from ninja.card_box import render_card_text  # noqa: E402
from scripts.theme import Theme, load_theme  # noqa: E402

DEFAULT_TIMEOUT = 120
AVATAR_SIZE = 220

# Corners used by the Ninja overlay; top-right (notification corner) excluded.
_CORNERS = [
    ("top-left", 0.02, 0.02),
    ("bottom-left", 0.02, 0.98),
    ("bottom-right", 0.98, 0.98),
]

# Global corner cycle so successive approvals do not stack on top of each other.
_corner_index = 0

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


def _build_css(theme: Theme) -> bytes:
    return f"""
window {{
    background-color: {theme.background};
}}
#panel {{
    background-color: {theme.surface};
    border: 2px solid {theme.accent};
    border-radius: 16px;
    padding: 14px;
}}
#title {{
    color: {theme.accent};
    font-family: monospace;
    font-size: 14px;
    font-weight: bold;
    margin-bottom: 8px;
}}
#card {{
    color: {theme.text};
    font-family: monospace;
    font-size: 12px;
    margin-bottom: 12px;
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
    padding: 8px 12px;
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


def _normalize_card(card: dict | str) -> str:
    if isinstance(card, dict):
        return render_card_text(card)
    return str(card)


def _next_corner() -> tuple[str, float, float]:
    global _corner_index
    corner = _CORNERS[_corner_index % len(_CORNERS)]
    _corner_index = (_corner_index + 1) % len(_CORNERS)
    return corner


class AvatarApprovalWindow:
    """One avatar + card + buttons approval window.  Fail-closed."""

    WIDTH = 640
    HEIGHT = 360

    def __init__(self, card: str, agent: str = "ninja", timeout: int = DEFAULT_TIMEOUT):
        self.card = card
        self.agent = agent
        self.timeout = max(1, timeout)
        self.result = "deny"
        self._closed = False
        self._timer_id: int | None = None
        self._window: Any | None = None
        self._theme = load_theme()

        if not _GTK_AVAILABLE:
            return

        display = Gdk.Display.get_default()
        if display is None and "DISPLAY" not in os.environ:
            os.environ["DISPLAY"] = ":0"
        try:
            display = Gdk.Display.get_default()
        except Exception:
            display = None
        if display is None:
            return

        self._build_ui()

    def _build_ui(self) -> None:
        self._window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self._window.set_title(f"{self.agent.title()} approval")
        self._window.set_default_size(self.WIDTH, self.HEIGHT)
        self._window.set_keep_above(True)
        self._window.set_decorated(False)
        self._window.set_skip_taskbar_hint(True)
        self._window.set_skip_pager_hint(True)
        self._window.set_destroy_with_parent(True)

        self._window.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self._window.connect("delete-event", self._on_close)
        self._window.connect("key-press-event", self._on_key_press)

        screen = self._window.get_screen()
        provider = Gtk.CssProvider()
        provider.load_from_data(_build_css(self._theme))
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        panel = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        panel.set_name("panel")
        self._window.add(panel)

        # Avatar
        avatar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        avatar = self._make_avatar()
        avatar_box.pack_start(avatar, False, False, 0)
        agent_label = Gtk.Label(label=self.agent.upper())
        agent_label.set_name("title")
        avatar_box.pack_start(agent_label, False, False, 0)
        panel.pack_start(avatar_box, False, False, 0)

        # Right side: card + buttons
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        panel.pack_start(right, True, True, 0)

        title = Gtk.Label(label=f"{self.agent.upper()} APPROVAL REQUIRED")
        title.set_name("title")
        title.set_xalign(0.0)
        right.pack_start(title, False, False, 0)

        card_label = Gtk.Label(label=self.card)
        card_label.set_name("card")
        card_label.set_xalign(0.0)
        card_label.set_yalign(0.0)
        card_label.set_line_wrap(True)
        card_label.set_line_wrap_mode(2)
        right.pack_start(card_label, True, True, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_valign(Gtk.Align.END)
        btn_box.set_homogeneous(True)
        right.pack_start(btn_box, False, False, 0)

        labels = {
            "once": "Approve once",
            "session": "Approve session",
            "always": "Approve always",
            "deny": "Deny",
        }
        for key, label in labels.items():
            btn = Gtk.Button(label=label)
            btn.set_can_focus(False)
            btn.connect("clicked", self._on_clicked, key)
            btn_box.pack_start(btn, True, True, 0)

        self._position_window()

    def _make_avatar(self) -> Any:
        path = _avatar_path(self.agent)
        if path:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(path), AVATAR_SIZE, AVATAR_SIZE, True
                )
                img = Gtk.Image.new_from_pixbuf(pixbuf)
                return img
            except Exception:
                pass
        # Fallback: colored badge with initial.
        fallback = Gtk.Label(label=self.agent[0].upper() if self.agent else "?")
        fallback.set_name("title")
        fallback.set_size_request(AVATAR_SIZE, AVATAR_SIZE)
        return fallback

    def _position_window(self) -> None:
        screen = self._window.get_screen()
        sw = screen.get_width()
        sh = screen.get_height()
        name, fx, fy = _next_corner()
        x = int((sw - self.WIDTH) * fx)
        y = int((sh - self.HEIGHT) * fy)
        # Clamp just in case.
        x = max(0, min(x, sw - self.WIDTH))
        y = max(0, min(y, sh - self.HEIGHT))
        self._window.move(x, y)

    def _on_close(self, _widget, _event) -> bool:
        self._finish("deny")
        return True

    def _on_key_press(self, _widget, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._finish("deny")
            return True
        return False

    def _on_clicked(self, button: Any, key: str) -> None:
        if self._closed:
            return
        self._animate_choice(button, key)

    def _animate_choice(self, button: Any, key: str) -> None:
        ctx = button.get_style_context()
        ctx.add_class("selected")
        GLib.timeout_add(120, self._flash_success, button, key)

    def _flash_success(self, button: Any, key: str) -> bool:
        if self._closed:
            return False
        ctx = button.get_style_context()
        ctx.remove_class("selected")
        ctx.add_class("success")
        GLib.timeout_add(180, self._after_flash, key)
        return False

    def _after_flash(self, key: str) -> bool:
        if self._closed:
            return False
        if key == "always":
            self._confirm_always()
        else:
            self._finish(key)
        return False

    def _confirm_always(self) -> None:
        confirm = Gtk.MessageDialog(
            parent=self._window,
            flags=Gtk.DialogFlags.MODAL,
            type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            message_format="Grant ALWAYS approval for this run?",
        )
        confirm.set_title("Confirm always approval")
        confirm.format_secondary_text("This will persist for the remainder of the run.")
        response = confirm.run()
        confirm.destroy()
        if response == Gtk.ResponseType.YES:
            self._finish("always")
        else:
            self._finish("deny")

    def _finish(self, verdict: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = verdict
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        if self._window is not None:
            self._window.destroy()
            self._window = None
        Gtk.main_quit()

    def run(self) -> str:
        if not _GTK_AVAILABLE or self._window is None:
            return "deny"
        self._window.show_all()
        self._timer_id = GLib.timeout_add_seconds(self.timeout, self._on_timeout)
        Gtk.main()
        return self.result

    def _on_timeout(self) -> bool:
        if not self._closed:
            self._finish("deny")
        return False


def ask_avatar_approval(
    card: dict | str,
    agent: str = "ninja",
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Show the avatar approval surface and return the verdict.

    ``agent`` is one of "ninja" / "tahuti" / "viking".  ``card`` may be the
    queue-mapped dict used by card_box or the plain-English string Tahuti
    currently builds.  Fail-closed: any error or missing display returns
    "deny".
    """
    try:
        text = _normalize_card(card)
        dialog = AvatarApprovalWindow(text, agent=agent, timeout=timeout)
        return dialog.run()
    except Exception:
        return "deny"


def _selftest() -> int:
    print("import ok")
    card = {
        "who": "Tahuti agent",
        "what": "self-test construct",
        "where": "local",
        "when": "now",
        "how": "import + build widgets",
        "question": "Self-test approval?",
        "executor": "Test harness",
        "model": "deterministic",
    }
    dialog = AvatarApprovalWindow(_normalize_card(card), agent="ninja")
    if dialog._window is None:
        print(
            "SKIP: no GTK display available; headless-safe construct ok"
            + (f" ({_GTK_ERROR})" if _GTK_ERROR else "")
        )
    else:
        print("construct ok (display available)")
    print("selftest PASS")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    test_card = (
        "WHO: Tahuti agent\n"
        "WHAT: open calculator\n"
        "WHERE: desktop\n"
        "WHEN: now\n"
        "HOW: run_command\n"
        "DELEGATED TO: muse-spark-1.1"
    )
    print(ask_avatar_approval(test_card, agent="viking"))
