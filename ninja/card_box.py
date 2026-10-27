#!/usr/bin/env python3
"""Thootie spawned box — one small card beside the avatar.

Display contract (operator spec 2026-10-27): compact, anchored to the
avatar's current corner, one at a time, dismissible, never modal, never
blocking the user's own work.

Stage 2 shows card content passed in as a dict; live approve/deny
wiring lands after the operator has seen the read-only card work.
"""

from __future__ import annotations

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    _GTK_AVAILABLE = True
except Exception:  # pragma: no cover - GTK may be absent during py_compile
    _GTK_AVAILABLE = False
    Gtk = None  # type: ignore

from scripts.theme import css, load_theme


def card_position(
    avatar_x: int,
    avatar_y: int,
    avatar_size: int,
    corner: str,
    card_w: int,
    card_h: int,
    screen_w: int,
    screen_h: int,
    gap: int = 12,
) -> tuple[int, int]:
    """Anchor the card beside the avatar, clamped on-screen.

    ``corner`` is one of "top-left" / "bottom-left" / "bottom-right".
    Left-side avatars spawn the card to their right; the bottom-right
    avatar spawns it to its left so it never covers the screen edge.
    """
    if corner == "bottom-right":
        x = avatar_x - gap - card_w
    else:
        x = avatar_x + avatar_size + gap
    y = avatar_y
    x = max(0, min(x, screen_w - card_w))
    y = max(0, min(y, screen_h - card_h))
    return (x, y)


def render_card_text(card: dict) -> str:
    """Render the plain-English card body from queue-mapped fields."""
    lines = [
        f"QUESTION: {card.get('question', '')}",
        "",
        f"Who: {card.get('who', '')}",
        f"What: {card.get('what', '')}",
        f"Where: {card.get('where', '')}",
        f"When: {card.get('when', '')}",
        f"Why: {card.get('why', '')}",
        f"How: {card.get('how', '')}",
        "",
        f"Executor: {card.get('executor', '')}",
        f"Model: {card.get('model', '')}",
    ]
    return "\n".join(lines)


class ThootieCard:
    """One small dismissible card window. Opening a new card closes the old."""

    WIDTH = 340
    HEIGHT = 300

    _open: ThootieCard | None = None

    def __init__(self, card: dict, x: int, y: int):
        if not _GTK_AVAILABLE:
            raise RuntimeError("GTK3 not available")
        if ThootieCard._open is not None:
            ThootieCard._open.close()

        self._win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self._win.set_keep_above(True)
        self._win.set_decorated(False)
        self._win.set_skip_taskbar_hint(True)
        self._win.set_skip_pager_hint(True)
        self._win.set_default_size(self.WIDTH, self.HEIGHT)

        self._theme = load_theme()
        provider = Gtk.CssProvider()
        provider.load_from_data(css(self._theme).encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            self._win.get_screen(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(10)
        self._win.add(box)

        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(render_card_text(card))
        box.pack_start(view, True, True, 0)

        close = Gtk.Button(label="Dismiss")
        close.connect("clicked", lambda _b: self.close())
        box.pack_start(close, False, False, 0)

        self._win.move(x, y)
        ThootieCard._open = self

    def show(self):
        self._win.show_all()

    def close(self):
        if ThootieCard._open is self:
            ThootieCard._open = None
        self._win.destroy()
