#!/usr/bin/env python3
"""slideshow_uninstall.py — remove the slideshow app, optionally also pictures.

Usage:
  slideshow_uninstall.py        # interactive menu
  slideshow_uninstall.py app    # remove app only (keep pictures)
  slideshow_uninstall.py all    # remove app AND wipe pictures folder

Both options confirm before any destructive action. Safe to run multiple times.
"""

import sys
from pathlib import Path

SCRIPTS_DIR = Path.home() / "scripts"
PICTURES_DIR = Path.home() / "Pictures" / "slideshow"
APP_FILES = [
    SCRIPTS_DIR / "slideshow.py",
    SCRIPTS_DIR / "slideshow_uninstall.py",
]


def confirm(msg):
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def remove_app():
    removed = []
    for f in APP_FILES:
        if f.exists():
            f.unlink()
            removed.append(str(f))
    if removed:
        for r in removed:
            print(f"  removed {r}")
    else:
        print("  no app files found to remove")


def remove_pictures():
    if not PICTURES_DIR.exists():
        print(f"  picture folder not found: {PICTURES_DIR}")
        return
    pics = list(PICTURES_DIR.iterdir())
    print(f"  removing {len(pics)} entries from {PICTURES_DIR}...")
    for p in pics:
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                # only remove empty subdirs to be safe
                p.rmdir()
        except OSError as e:
            print(f"  could not remove {p}: {e}")
    try:
        PICTURES_DIR.rmdir()
        print(f"  removed folder {PICTURES_DIR}")
    except OSError:
        print(f"  folder {PICTURES_DIR} not empty after pass — kept it")


def do_app_only():
    print("\nThis will remove:")
    for f in APP_FILES:
        print(f"  {f}")
    print(f"Your pictures in {PICTURES_DIR} will be LEFT UNTOUCHED.\n")
    if not confirm("Proceed?"):
        print("cancelled")
        return
    remove_app()
    print("done.")


def do_everything():
    print("\nThis will remove:")
    for f in APP_FILES:
        print(f"  {f}")
    print(f"AND DELETE all pictures in {PICTURES_DIR}.")
    print("This cannot be undone.\n")
    if not confirm("Proceed?"):
        print("cancelled")
        return
    remove_pictures()
    remove_app()
    print("done.")


def menu():
    print("Slideshow uninstaller")
    print("  1) uninstall app ONLY (keep your pictures)")
    print("  2) uninstall EVERYTHING (app + pictures folder)")
    print("  3) cancel")
    try:
        choice = input("Choice [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "3"
    if choice == "1":
        do_app_only()
    elif choice == "2":
        do_everything()
    else:
        print("cancelled")


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ("-h", "--help"):
            print(__doc__.strip())
            return
        if arg == "app":
            do_app_only()
        elif arg == "all":
            do_everything()
        else:
            print(f"unknown arg: {arg}")
            print("usage: slideshow_uninstall.py [app|all]")
            sys.exit(1)
    else:
        menu()


if __name__ == "__main__":
    main()
