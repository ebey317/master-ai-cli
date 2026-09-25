#!/usr/bin/env python3
"""iprice — look up iTunes Store prices for albums or songs.

Pulls live from iTunes Search API (public, no auth needed). For deciding
what's worth BUYING instead of renting via subscription. Pairs with the
Radio app vision (own-it-or-buy-it model) — see project_radio_app.md.

Usage:
  iprice "michael jackson thriller"        # default: album, top 3
  iprice -s "thriller"                     # song instead of album
  iprice -n 5 "the beatles"                # top 5 results
  iprice -s -n 10 "fleetwood mac landslide"

Output columns: name — artist — price — track count (album) or album (song).
"""

import json
import sys
import urllib.parse
import urllib.request


def fetch(query, entity="album", limit=3):
    url = (
        f"https://itunes.apple.com/search"
        f"?term={urllib.parse.quote(query)}&entity={entity}&limit={limit}"
    )
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.loads(r.read())


def main():
    args = sys.argv[1:]
    entity = "album"
    limit = 3
    # Parse leading flags (-s for song, -n N for limit)
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag == "-s":
            entity = "song"
        elif flag == "-n" and args:
            try:
                limit = max(1, min(25, int(args.pop(0))))
            except ValueError:
                print("error: -n needs a number")
                sys.exit(1)
        elif flag in ("-h", "--help"):
            print(__doc__.strip())
            sys.exit(0)
        else:
            print(f"unknown flag: {flag}")
            sys.exit(1)
    if not args:
        print("usage: iprice [-s] [-n N] <search terms>")
        print("       iprice --help")
        sys.exit(1)
    query = " ".join(args)
    try:
        data = fetch(query, entity=entity, limit=limit)
    except Exception as e:
        print(f"error: {e}")
        sys.exit(1)
    results = data.get("results", [])
    if not results:
        print(f"no results for: {query}")
        return
    for r in results:
        if entity == "album":
            name = (r.get("collectionName") or "?")[:50]
            artist = (r.get("artistName") or "?")[:25]
            price = r.get("collectionPrice")
            price_s = f"${price:.2f}" if isinstance(price, (int, float)) else "?"
            tracks = r.get("trackCount", "?")
            print(f"{name:50s}  {artist:25s}  {price_s:>7s}  ({tracks} tracks)")
        else:
            name = (r.get("trackName") or "?")[:50]
            artist = (r.get("artistName") or "?")[:25]
            price = r.get("trackPrice")
            price_s = f"${price:.2f}" if isinstance(price, (int, float)) else "?"
            album = (r.get("collectionName") or "?")[:30]
            print(f"{name:50s}  {artist:25s}  {price_s:>7s}  (from {album})")


if __name__ == "__main__":
    main()
