#!/usr/bin/env python3
"""funnel_check.py — read-only money-funnel state for the suggestive-thinking
MONEY/ORG source (2026-10-24, Lijah's standard: "nobody said you're this far
off").

His point, verbatim sense: the surfaced suggestions said "want me to check?"
but never quantified the DISTANCE between "product exists" and "for sale."
The loop's job is to state the distance in flips, with receipts. This script
is the check both money items run on approval; it is READ-ONLY (Gumroad GET,
git/gh reads) and never prints the API key.

Modes:
    gumroad        — the storefront funnel: how many products, how many
                     PUBLISHED, how many carry files, how many have sold;
                     flips-to-live line for the dark ones.
    ai-controller  — the release funnel: tag on origin/master, GitHub
                     release assets, Gumroad attach + published state;
                     flips-to-sale line.

Testability: --json-file (gumroad mode: inject products JSON instead of the
live API) and --repo (ai-controller mode: alternate repo path). Tests never
touch the real key or the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

KEYS_FILE = Path(
    os.environ.get("FUNNEL_KEYS_FILE") or str(Path.home() / ".master_ai_keys")
)
GUMROAD_API = "https://api.gumroad.com/v2/products"
AI_CONTROLLER_REPO = Path.home() / "ai-controller"


def _load_key() -> str | None:
    try:
        for line in KEYS_FILE.read_text().splitlines():
            if line.startswith("GUMROAD_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def _gumroad_products() -> list[dict] | None:
    """Live GET, key never printed. Returns None on any failure — the caller
    prints an honest reason rather than a fabricated funnel."""
    key = _load_key()
    if not key:
        print(f"  (no GUMROAD_API_KEY line in {KEYS_FILE} — cannot read the store)")
        return None
    import urllib.request

    req = urllib.request.Request(
        GUMROAD_API, headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  (store read failed: {type(e).__name__} — not reporting fake numbers)")
        return None
    return data.get("products", [])


def print_gumroad_funnel(products: list[dict]) -> None:
    total = len(products)
    published = [p for p in products if p.get("published")]
    dark = [p for p in products if not p.get("published")]
    attached = [p for p in products if (p.get("attached_files") or [])]
    sales = sum(int(p.get("sales_count") or 0) for p in products)
    for p in products:
        n_files = len(p.get("attached_files") or [])
        price = (int(p.get("price") or 0)) / 100
        state = "LIVE" if p.get("published") else "DARK"
        attach_state = f"{n_files} file(s)" if n_files else "NO FILE ATTACHED"
        sold = p.get("sales_count") or 0
        print(
            f"  - {p.get('name')} — ${price:.2f} — {state}, {attach_state}, {sold} sold"
        )
    print(
        f"store: {total} product(s), {len(published)} published, "
        f"{len(attached)} with deliverable files, {sales} total sales"
    )
    if dark:
        print(
            "distance: unpublished product means a buyer cannot buy it — "
            f"{len(dark)} product(s) are one attach + one publish flip from live"
        )
    attached_dark = [
        p
        for p in products
        if not p.get("published") and (p.get("attached_files") or [])
    ]
    for p in attached_dark:
        print(f"  attached-but-dark: {p.get('name')} — ONE publish flip from live")


def ai_controller_funnel(repo: Path) -> None:
    # 1. tag
    tag = None
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "tag", "-l", "v*"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        tags = out.stdout.split()
        tag = tags[-1] if tags else None
    except Exception as e:
        print(f"  (tag read failed: {type(e).__name__} — not reporting fake numbers)")
    print(f"  repo: {'tagged ' + tag if tag else 'NO release tag'}")
    # 2. release assets
    assets_ok = False
    n_assets = 0
    if tag:
        try:
            rel = subprocess.run(
                [
                    "gh",
                    "release",
                    "view",
                    tag,
                    "--repo",
                    "ebey317/ai-controller",
                    "--json",
                    "assets",
                    "--jq",
                    ".assets | length",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            n_assets = int(rel.stdout.strip() or 0)
            assets_ok = n_assets > 0
        except Exception:
            pass
    print(
        f"  release: {'OK (' + str(n_assets) + ' assets)' if assets_ok else 'NO release assets live'}"
    )
    # 3. gumroad product state (share the products read)
    products = _gumroad_products()
    if products is not None:
        mine = next(
            (
                p
                for p in products
                if "ai-controller" in str(p.get("custom_permalink", ""))
            ),
            None,
        )
        if mine:
            n_files = len(mine.get("attached_files") or [])
            pub = bool(mine.get("published"))
            sold = mine.get("sales_count") or 0
            print(
                f"  gumroad: {'file attached' if n_files else 'NO file attached'}, "
                f"{'PUBLISHED' if pub else 'DARK'}, {sold} sold"
            )
            flips = (0 if n_files else 1) + (0 if pub else 1)
            print(f"  FLIPS TO SALE: {flips}")
        else:
            print("  gumroad: no ai-controller product listing found — one to CREATE")
    else:
        print("  gumroad: unreadable — see reason above")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "gumroad":
        if "--json-file" in sys.argv:
            jf = Path(sys.argv[sys.argv.index("--json-file") + 1])
            try:
                data = json.loads(jf.read_text())
            except Exception as e:
                print(
                    f"  (cannot read {jf}: {type(e).__name__} — not reporting fake numbers)"
                )
                return 1
            print_gumroad_funnel(data.get("products", []))
        else:
            products = _gumroad_products()
            if products is None:
                return 1
            print_gumroad_funnel(products)
        return 0
    if mode == "ai-controller":
        repo = AI_CONTROLLER_REPO
        if "--repo" in sys.argv:
            repo = Path(sys.argv[sys.argv.index("--repo") + 1])
        ai_controller_funnel(repo)
        return 0
    print(
        "usage: funnel_check.py {gumroad|ai-controller} [--json-file FILE | --repo PATH]"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
