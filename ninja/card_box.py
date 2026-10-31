#!/usr/bin/env python3
"""Thootie spawned box — card anchored beside avatar, same window family.

The primary approval surface lives inside ThootieOverlay (same GTK window,
horizontal box: avatar left, card right). ThootieCard here is retained as a
legacy anchored helper that spawns FROM the avatar window family — it
calculates its position via card_position() anchored beside the avatar and
uses only theme palette, transparent visuals for crisp edges.
"""

from __future__ import annotations

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gtk

    _GTK_AVAILABLE = True
except Exception:  # pragma: no cover
    _GTK_AVAILABLE = False
    Gtk = None  # type: ignore
    Gdk = None  # type: ignore

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
    gap: int = 14,
) -> tuple[int, int]:
    """Anchor the card beside the avatar, clamped on-screen.

    Left-side avatars spawn to the right; bottom-right spawns to left
    so it never covers the edge. Gap keeps breathing room for float.
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
    """Anchored card that spawns FROM avatar overlay.

    Same window family: positioned beside the avatar's current corner,
    clamped on-screen, theme-palette only, no gray boxes, clean transparent
    window with RGBA visual for crisp edges.
    """

    WIDTH = 360
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

        # Crisp edges: RGBA visual so un-painted regions are transparent
        screen = self._win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self._win.set_visual(visual)
        self._win.set_app_paintable(True)

        self._theme = load_theme()
        provider = Gtk.CssProvider()
        provider.load_from_data(css(self._theme).encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            self._win.get_screen(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
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

    @classmethod
    def spawn_from_avatar(cls, card: dict, avatar_overlay) -> ThootieCard:
        """Spawn anchored beside avatar_overlay — same window family positioning."""
        try:
            ax, ay = avatar_overlay.current_position()
            corner = avatar_overlay.current_corner()
            screen = avatar_overlay._win.get_screen()
            sw = screen.get_width()
            sh = screen.get_height()
            x, y = card_position(
                ax,
                ay,
                avatar_overlay.IDLE_SIZE,
                corner,
                cls.WIDTH,
                cls.HEIGHT,
                sw,
                sh,
                gap=14,
            )
        except Exception:
            x, y = 40, 40
        return cls(card, x, y)

    def show(self):
        self._win.show_all()

    def close(self):
        if ThootieCard._open is self:
            ThootieCard._open = None
        self._win.destroy()
