#!/usr/bin/env python3
"""Corner Tahuti ibis overlay."""

import argparse
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

ASSET = os.path.join(os.path.dirname(__file__), "assets", "tahuti_avatar.png")
SIZE = 96
DURATION_S = 15
MARGIN = 12


def show_ibis():
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
        ASSET, SIZE, SIZE, preserve_aspect_ratio=True
    )
    win = Gtk.Window(type=Gtk.WindowType.POPUP)
    win.set_decorated(False)
    win.set_skip_taskbar_hint(True)
    win.set_keep_above(True)
    win.set_resizable(False)
    win.add(Gtk.Image.new_from_pixbuf(pixbuf))
    win.show_all()

    screen = Gdk.Screen.get_default()
    sw, sh = screen.get_width(), screen.get_height()
    # Fixed SIZE: get_size() pre-realize returns 0 and parks us off-screen.
    # Top-right keeps clear of the bottom panel.
    win.move(sw - SIZE - MARGIN, MARGIN)

    GLib.timeout_add_seconds(DURATION_S, Gtk.main_quit)
    win.connect("destroy", Gtk.main_quit)
    Gtk.main()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show Tahuti ibis corner overlay.")
    parser.add_argument(
        "--test", action="store_true", help="Show overlay for 15s and exit"
    )
    args = parser.parse_args(argv)
    if not args.test:
        parser.print_help()
        return 1
    show_ibis()
    return 0


if __name__ == "__main__":
    sys.exit(main())
