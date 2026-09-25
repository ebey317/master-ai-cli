#!/usr/bin/env python3
"""whereisit — find an album across DRM-free + streaming stores.

Builds ready-to-click search URLs for the major music sources. Use this
when iTunes (iprice) doesn't have an album — Section.80, indie rappers,
out-of-print catalogs, etc. Pairs with iprice + the Radio app vision.

Usage:
  whereisit "kendrick lamar section 80"     # print URLs
  whereisit -o "rick ross trilla"           # ALSO open all in browser
  whereisit --help

Stores covered (in order):
  - Bandcamp        — DRM-free, indie-heavy, often direct-from-artist
  - Discogs         — vinyl + CD marketplace, best for out-of-print
  - Amazon Music MP3 — DRM-free MP3 store, broader catalog than iTunes
  - 7digital        — DRM-free MP3 store, sometimes has what others don't
  - Apple Music     — streaming reference (subscription-only listen)
  - YouTube Music   — streaming reference (free tier with ads)
"""

import subprocess
import sys
import urllib.parse

STORES = [
    ("Bandcamp", "https://bandcamp.com/search?q={q}&item_type=a"),
    ("Discogs", "https://www.discogs.com/search/?q={q}&type=master"),
    ("Amazon Music MP3", "https://www.amazon.com/s?k={q}&i=digital-music"),
    ("7digital", "https://us.7digital.com/search?q={q}"),
    ("Apple Music", "https://music.apple.com/us/search?term={q}"),
    ("YouTube Music", "https://music.youtube.com/search?q={q}"),
]


def main():
    args = sys.argv[1:]
    open_browser = False
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag == "-o":
            open_browser = True
        elif flag in ("-h", "--help"):
            print(__doc__.strip())
            sys.exit(0)
        else:
            print(f"unknown flag: {flag}")
            sys.exit(1)
    if not args:
        print("usage: whereisit [-o] <search terms>")
        print("       whereisit --help")
        sys.exit(1)
    query = " ".join(args)
    q = urllib.parse.quote(query)
    print(f"Search: {query}\n")
    for label, template in STORES:
        url = template.format(q=q)
        print(f"  {label:18s}  {url}")
        if open_browser:
            try:
                subprocess.Popen(
                    ["xdg-open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                print(f"    (open failed: {e})")


if __name__ == "__main__":
    main()
